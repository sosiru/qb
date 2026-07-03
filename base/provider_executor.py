from django.conf import settings

from base.models import PaymentBatch, PaymentInstruction
from .providers.pesaway import PesaWayAPIClient, PesaWayAPIError
from base.services import (
    amount_minor_to_provider_amount,
    build_provider_reference,
    mark_batch_collection_complete,
    record_batch_failure,
    record_instruction_failure,
    record_instruction_success,
)


def build_pesaway_client():
    if not settings.PESAWAY_RESULTS_URL:
        raise PesaWayAPIError("PESAWAY_RESULTS_URL must be configured for real provider calls.")
    return PesaWayAPIClient(
        client_id=settings.PESAWAY_CLIENT_ID,
        client_secret=settings.PESAWAY_CLIENT_SECRET,
        base_url=settings.PESAWAY_BASE_URL,
        timeout=settings.PESAWAY_TIMEOUT_SECONDS,
    )


def _extract_provider_reference(response):
    if not isinstance(response, dict):
        return ""
    candidates = [
        response.get("transaction_reference"),
        response.get("TransactionReference"),
        response.get("transaction_id"),
        response.get("TransactionID"),
    ]
    data = response.get("data")
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("transaction_reference"),
                data.get("TransactionReference"),
                data.get("transaction_id"),
                data.get("TransactionID"),
            ]
        )
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return ""


def request_collection_for_batch(batch_id):
    batch = PaymentBatch.objects.select_related("user").get(id=batch_id)
    if not batch.user_id or not batch.user.phone_number:
        raise PesaWayAPIError("STK collection requires a user phone number.")

    client = build_pesaway_client()
    provider_reference = build_provider_reference("collect", batch.id)
    response = client.receive_c2b_payment(
        external_reference=provider_reference,
        amount=amount_minor_to_provider_amount(batch.total_amount_minor + batch.fee_amount_minor),
        phone_number=batch.user.phone_number,
        channel=settings.PESAWAY_C2B_CHANNEL,
        reason=batch.description or "Route payment collection",
        results_url=settings.PESAWAY_RESULTS_URL,
    )
    batch.metadata["collection_reference"] = provider_reference
    batch.metadata["collection_response"] = response
    batch.save(update_fields=["metadata", "updated_at"])
    mark_batch_collection_complete(batch, response)
    return response


def dispatch_instruction(instruction_id):
    instruction = PaymentInstruction.objects.select_related("batch").get(id=instruction_id)
    client = build_pesaway_client()
    provider_reference = build_provider_reference("payout", instruction.id)
    amount = amount_minor_to_provider_amount(instruction.amount_minor)
    reason = instruction.batch.description or instruction.category or "Route payout"

    if instruction.recipient_type == "MOBILE":
        response = client.send_b2c_payment(
            external_reference=provider_reference,
            amount=amount,
            phone_number=instruction.destination.get("phone_number", ""),
            channel=settings.PESAWAY_B2C_CHANNEL,
            reason=reason,
            results_url=settings.PESAWAY_RESULTS_URL,
        )
    elif instruction.recipient_type == "BANK":
        response = client.send_bank_payment(
            external_reference=provider_reference,
            amount=amount,
            account_number=instruction.destination.get("account_number", ""),
            channel=settings.PESAWAY_BANK_CHANNEL,
            bank_code=instruction.destination.get("bank_code", ""),
            currency=settings.PESAWAY_DEFAULT_CURRENCY,
            reason=reason,
            results_url=settings.PESAWAY_RESULTS_URL,
        )
    elif instruction.recipient_type == "PAYBILL":
        response = client.send_b2b_payment(
            external_reference=provider_reference,
            amount=amount,
            account_number=instruction.destination.get("paybill_number", ""),
            channel=settings.PESAWAY_B2B_PAYBILL_CHANNEL,
            reason=reason,
            results_url=settings.PESAWAY_RESULTS_URL,
        )
    elif instruction.recipient_type == "TILL":
        response = client.send_b2b_payment(
            external_reference=provider_reference,
            amount=amount,
            account_number=instruction.destination.get("till_number", ""),
            channel=settings.PESAWAY_B2B_TILL_CHANNEL,
            reason=reason,
            results_url=settings.PESAWAY_RESULTS_URL,
        )
    else:
        raise PesaWayAPIError(f"Unsupported recipient type {instruction.recipient_type}.")

    extracted_reference = _extract_provider_reference(response) or provider_reference
    record_instruction_success(instruction, response, provider_reference=extracted_reference)
    return response


def process_outbox_event(event):
    if event.topic == "collection.stk.requested":
        request_collection_for_batch(event.aggregate_id)
    elif event.topic == "payment.instruction.dispatch":
        dispatch_instruction(event.aggregate_id)
    elif event.topic in {
        "wallet.topup.completed",
        "payment.batch.succeeded",
        "payment.batch.failed",
        "payment.batch.partial",
    }:
        return
    else:
        raise PesaWayAPIError(f"Unsupported outbox topic {event.topic}.")


def fail_instruction_event(event, exc):
    if event.aggregate_type != "payment_instruction":
        if event.aggregate_type == "payment_batch":
            batch = PaymentBatch.objects.get(id=event.aggregate_id)
            record_batch_failure(batch, str(exc))
        return
    instruction = PaymentInstruction.objects.get(id=event.aggregate_id)
    record_instruction_failure(instruction, str(exc), provider_response={"error": str(exc)})
