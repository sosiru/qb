import uuid
import random
import string
import threading


def generate_uuid():
    return uuid.uuid4()


class TransactionRefGenerator:
    """Generates unique transaction references with alphanumeric progression."""

    def __init__(self, prefix: str = "A", suffix: str = "0"):
        self.prefix = list(prefix)
        self.suffix = suffix
        self._lock = threading.Lock()

    def _increment_suffix(self) -> None:
        """Increment suffix using alphanumeric progression (0-9, A-Z)."""
        suffix = list(self.suffix)
        carry = True

        for i in range(len(suffix) - 1, -1, -1):
            if not carry:
                break

            if suffix[i] == "Z":
                suffix[i] = "0"
            elif suffix[i] == "9":
                suffix[i] = "A"
                carry = False
            else:
                suffix[i] = chr(ord(suffix[i]) + 1)
                carry = False

        if carry:
            suffix.insert(0, "0")

        self.suffix = "".join(suffix)

    def _increment_prefix(self) -> None:
        """Increment prefix when suffix overflows."""
        carry = True

        for i in range(len(self.prefix) - 1, -1, -1):
            if not carry:
                break

            if self.prefix[i] == "Z":
                self.prefix[i] = "A"
            else:
                self.prefix[i] = chr(ord(self.prefix[i]) + 1)
                carry = False

        if carry:
            self.prefix.insert(0, "A")

    def generate(self, random_length: int = 8) -> str:
        """Generate a unique transaction reference."""
        with self._lock:
            random_string = "".join(
                random.choices(string.ascii_uppercase + string.digits, k=random_length)
            )
            transaction_ref = f"{''.join(self.prefix)}{self.suffix}{random_string}"
            self._increment_suffix()
            if self.suffix == "0":
                self._increment_prefix()
            return transaction_ref
