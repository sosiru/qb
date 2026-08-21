import logging
from decimal import Decimal

from django.conf import settings
import requests
from base.models import PaymentBatch, PaymentInstruction
from ledger.models import PaymentRequest, Transaction as LedgerTransactionRecord
from ledger.services import PaymentService, get_or_create_user_account
from base.services import mark_batch_collection_complete, process_kplc_payout_notification, record_batch_failure, record_instruction_failure

logger = logging.getLogger(__name__)


class PaymentDispatchError(Exception):
    pass


def _sandbox_enabled():
    return not bool(getattr(settings, "PAYMENT_MICROSERVICE_URL", ""))


def _money_amount(value):
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def request_collection_for_batch(batch_id, payload=None):
    payload = payload or {}
    logger.info("payment_executor.collection.start batch_id=%s payload_keys=%s", batch_id, sorted(payload.keys()))
    batch = PaymentBatch.objects.select_related("user", "organization", "approved_by", "submitted_by").get(id=batch_id)
    actor = batch.user or batch.approved_by or batch.submitted_by
    if not actor:
        raise PaymentDispatchError("STK collection requires a user, submitter, or approver phone number.")
    amount_minor = _money_amount(payload.get("amount_minor") or batch.total_amount_minor + batch.fee_amount_minor)
    if amount_minor <= 0:
        raise PaymentDispatchError("STK collection amount must be greater than zero.")
    if batch.organization_id:
        from ledger.services import get_or_create_organization_account

        account = get_or_create_organization_account(batch.organization)
    else:
        account = get_or_create_user_account(actor)
    payment_request = PaymentService(sandbox=_sandbox_enabled()).initiate_stk_push(
        account,
        amount_minor=amount_minor,
        phone_number=payload.get("phone_number") or actor.phone_number,
        idempotency_key=f"quick-pay-stk-collection:{batch.id}",
        metadata={
            "batch_id": str(batch.id),
            "purpose": "batch_collection",
            "funding_reason": payload.get("reason") or "wallet_funding_before_payout",
        },
    )
    logger.info(
        "payment_executor.collection.submitted batch_id=%s payment_request_id=%s request_id=%s status=%s",
        batch.id,
        payment_request.id,
        payment_request.request_id,
        payment_request.status,
    )
    batch.refresh_from_db()
    batch.metadata["collection_request_id"] = payment_request.request_id
    batch.metadata["collection_originator_ref"] = payment_request.originator_ref
    if batch.metadata.get("collection_status") != "SUCCEEDED":
        batch.metadata["collection_status"] = payment_request.status
    batch.save(update_fields=["metadata", "updated_at"])
    if payment_request.status == payment_request.Status.COMPLETED and batch.metadata.get("collection_status") != "SUCCEEDED":
        mark_batch_collection_complete(batch, payment_request.response_payload)
    return payment_request.response_payload


def dispatch_instruction(instruction_id):
    logger.info("payment_executor.instruction.start instruction_id=%s", instruction_id)
    instruction = PaymentInstruction.objects.select_related("batch", "batch__user", "batch__organization").get(id=instruction_id)
    ledger_transaction_id = (instruction.batch.metadata or {}).get("ledger_transaction_id")
    ledger_transaction = LedgerTransactionRecord.objects.get(id=ledger_transaction_id) if ledger_transaction_id else None
    payment_request = PaymentService(sandbox=_sandbox_enabled()).initiate_instruction_payout(
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
        "payment_executor.instruction.submitted instruction_id=%s request_id=%s originator_ref=%s status=%s",
        instruction.id,
        payment_request.request_id,
        payment_request.originator_ref,
        payment_request.status,
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
        return request_collection_for_batch(event.aggregate_id, event.payload)
    if event.topic == "payment.instruction.dispatch":
        return dispatch_instruction(event.aggregate_id)
    if event.topic == "payment.instruction.kplc_notification":
        return process_kplc_payout_notification(event.aggregate_id)
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
        if event.topic == "payment.instruction.kplc_notification":
            logger.warning(
                "kplc.outbox.failed instruction_id=%s event_id=%s error=%s",
                event.aggregate_id,
                event.id,
                exc,
            )
            return
        instruction = PaymentInstruction.objects.get(id=event.aggregate_id)
        processing_request = PaymentRequest.objects.filter(
            operation=PaymentRequest.Operation.PAYOUT,
            request_payload__instruction_id=str(instruction.id),
            status=PaymentRequest.Status.PROCESSING,
        ).exists()
        if processing_request:
            instruction.microservice_response = {
                **(instruction.microservice_response or {}),
                "dispatch_error": str(exc),
                "dispatch_status": "UNKNOWN",
            }
            instruction.save(update_fields=["microservice_response", "updated_at"])
            logger.warning(
                "payment_executor.instruction.dispatch_unknown instruction_id=%s event_id=%s error=%s",
                instruction.id,
                event.id,
                exc,
            )
            return
        record_instruction_failure(instruction, str(exc), microservice_response={"error": str(exc)})
        return
    if event.aggregate_type == "payment_batch":
        batch = PaymentBatch.objects.get(id=event.aggregate_id)
        if event.topic == "collection.stk.requested":
            processing_request = PaymentRequest.objects.filter(
                operation=PaymentRequest.Operation.STK_PUSH,
                request_payload__metadata__batch_id=str(batch.id),
                status=PaymentRequest.Status.PROCESSING,
            ).exists()
            if processing_request:
                batch.metadata["collection_status"] = "UNKNOWN"
                batch.metadata["collection_dispatch_error"] = str(exc)[:255]
                batch.save(update_fields=["metadata", "updated_at"])
                logger.warning(
                    "payment_executor.collection.dispatch_unknown batch_id=%s event_id=%s error=%s",
                    batch.id,
                    event.id,
                    exc,
                )
                return
        record_batch_failure(batch, str(exc))
