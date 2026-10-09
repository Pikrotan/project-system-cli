"""Stage A fake only: no network, model, source processing or semantic evidence."""

class FakeTransportError(RuntimeError):
    pass


def dispatch(contract):
    scenario = contract['options']['scenario']
    if scenario in {'after_intent', 'timeout', 'unknown_delivery'}:
        raise FakeTransportError(scenario)
    if scenario == 'malformed_response':
        return None
    return b'{"fake_orchestration_only":true}'
