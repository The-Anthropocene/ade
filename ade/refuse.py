# id: ade.refuse
# what: the one way the kit says no: a code, its cause and the cure the reader runs next
# cmd: -
# reads: -
# writes: -


class Refusal(Exception):
    def __init__(self, code, cause, cure):
        super().__init__(f"{code}: {cause}")
        self.code, self.cause, self.cure = code, cause, cure

    def render(self):
        return f"ade refused [{self.code}]: {self.cause}\n  cure: {self.cure}"


def refuse(code, cause, cure):
    # Codes are literal strings at the call site so `ade budget` can count them from the source.
    if not cure:
        raise ValueError(f"refusal {code} has no cure; every refusal names one")
    raise Refusal(code, cause, cure)
