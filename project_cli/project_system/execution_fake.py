"""Stage A fake only: no network, model, source processing or semantic evidence."""

from hashlib import sha256


_RESPONSE = b'{"fake_orchestration_only":true}'


def response_commitment():
    """Pinned fake protocol descriptor; not an API for sealing caller responses."""
    return {'sha256': sha256(_RESPONSE).hexdigest(), 'bytes': len(_RESPONSE)}


class FakeTransportError(RuntimeError):
    pass


def dispatch(contract):
    scenario = contract['options']['scenario']
    if scenario in {'after_intent', 'timeout', 'unknown_delivery'}:
        raise FakeTransportError(scenario)
    if scenario == 'malformed_response':
        return None
    return _RESPONSE
