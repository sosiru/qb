import re


def normalize_phone_number(phone_number):
    digits = re.sub(r"\D", "", str(phone_number or ""))

    if not digits:
        return ""
    if digits.startswith("254"):
        return digits
    if digits.startswith("0") and len(digits) >= 10:
        return f"254{digits[1:]}"
    if len(digits) == 9:
        return f"254{digits}"
    return digits
