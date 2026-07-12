import logging

from django.conf import settings

from base.models import PaymentBatch, PaymentInstruction
from ledger.models import Transaction as LedgerTransactionRecord
from ledger.services import PaymentInterface, get_or_create_user_account
from base.services import mark_batch_collection_complete, record_batch_failure, record_instruction_failure

logger = logging.getLogger(__name__)


class PaymentDispatchError(Exception):
    pass


def _sandbox_enabled():
    return not bool(getattr(settings, "PAYMENT_MICROSERVICE_URL", ""))


def request_collection_for_batch(batch_id):
    batch = PaymentBatch.objects.select_related("user").get(id=batch_id)
    if not batch.user_id:
        raise PaymentDispatchError("STK collection requires a user-backed batch.")
    amount_minor = batch.total_amount_minor + batch.fee_amount_minor
    if amount_minor <= 0:
        raise PaymentDispatchError("STK collection amount must be greater than zero.")
    account = get_or_create_user_account(batch.user)
    payment_request = PaymentInterface(sandbox=_sandbox_enabled()).initiate_stk_push(
        account,
        amount_minor=amount_minor,
        phone_number=batch.user.phone_number,
        metadata={"batch_id": str(batch.id), "purpose": "batch_collection"},
    )
    batch.metadata["collection_request_id"] = payment_request.request_id
    batch.metadata["collection_originator_ref"] = payment_request.originator_ref
    batch.metadata["collection_status"] = payment_request.status
    batch.save(update_fields=["metadata", "updated_at"])
    if payment_request.status == payment_request.Status.COMPLETED:
        mark_batch_collection_complete(batch, payment_request.response_payload)
    return payment_request.response_payload


def dispatch_instruction(instruction_id):
    instruction = PaymentInstruction.objects.select_related("batch", "batch__user", "batch__organization").get(id=instruction_id)
    ledger_transaction_id = (instruction.batch.metadata or {}).get("ledger_transaction_id")
    ledger_transaction = LedgerTransactionRecord.objects.get(id=ledger_transaction_id) if ledger_transaction_id else None
    payment_request = PaymentInterface(sandbox=_sandbox_enabled()).initiate_instruction_payout(
        instruction,
        transaction_record=ledger_transaction,
        metadata={"batch_id": str(instruction.batch_id), "instruction_id": str(instruction.id)},
    )
    instruction.microservice_request_id = payment_request.request_id or payment_request.originator_ref
    instruction.microservice_response = {
        **(instruction.microservice_response or {}),
        "request_id": payment_request.request_id,
        "originator_ref": payment_request.originator_ref,
        "submission_response": payment_request.response_payload,
        "submission_status": payment_request.status,
    }
    instruction.save(update_fields=["microservice_request_id", "microservice_response", "updated_at"])
    logger.info(
        "payment_microservice.payout.submitted instruction_id=%s request_id=%s originator_ref=%s",
        instruction.id,
        payment_request.request_id,
        payment_request.originator_ref,
    )
    return payment_request.response_payload


def process_outbox_event(event):
    logger.info(
        "outbox.process.start event_id=%s topic=%s aggregate_type=%s aggregate_id=%s",
        event.id,
        event.topic,
        event.aggregate_type,
        event.aggregate_id,
    )
    if event.topic == "collection.stk.requested":
        if event.aggregate_type != "payment_batch":
            raise PaymentDispatchError(f"Unsupported collection aggregate type {event.aggregate_type}.")
        return request_collection_for_batch(event.aggregate_id)
    if event.topic == "payment.instruction.dispatch":
        return dispatch_instruction(event.aggregate_id)
    if event.topic in {
        "wallet.topup.completed",
        "payment.batch.succeeded",
        "payment.batch.failed",
        "payment.batch.partial",
    }:
        return None
    raise PaymentDispatchError(f"Unsupported outbox topic {event.topic}.")


def fail_instruction_event(event, exc):
    if event.aggregate_type == "payment_instruction":
        instruction = PaymentInstruction.objects.get(id=event.aggregate_id)
        record_instruction_failure(instruction, str(exc), microservice_response={"error": str(exc)})
        return
    if event.aggregate_type == "payment_batch":
        batch = PaymentBatch.objects.get(id=event.aggregate_id)
        record_batch_failure(batch, str(exc))
