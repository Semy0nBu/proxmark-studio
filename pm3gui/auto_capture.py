"""Identification and presence tracking for unattended, read-only intake."""
from dataclasses import dataclass
import re
from .operations import Detection, parse_detection
from .studio_data import lf_identity


class UnclearScan(ValueError):
    pass


class MultipleCards(UnclearScan):
    pass


@dataclass(frozen=True)
class IntakeCard:
    band: str
    protocol: str
    uid: str = ''
    kind: str = 'detection'
    classic_size: str = ''

    @property
    def token(self):
        return self.band, self.protocol, self.uid.upper(), self.kind, self.classic_size

    @property
    def presence_key(self):
        # A more/less detailed type hint must not reimport the same UID.
        return (self.band, self.uid.upper()) if self.uid else self.token

    def legacy_detection(self):
        return Detection(self.kind, self.uid, self.protocol, self.classic_size, True)


class PresenceGate:
    """One attempt per presentation; two explicit empty scans rearm a band."""
    def __init__(self):
        self.tokens = {}
        self.misses = {'hf': 0, 'lf': 0}

    def observe(self, band, card):
        if band not in self.misses or card is not None and card.band != band:
            raise ValueError('Invalid intake band.')
        if card is None:
            self.misses[band] += 1
            if self.misses[band] >= 2:
                self.tokens.pop(band, None)
            return False
        self.misses[band] = 0
        if self.tokens.get(band) == card.presence_key:
            return False
        self.tokens[band] = card.presence_key
        return True

    def unclear(self, band):
        self.misses[band] = 0


def transport_failed(output, code):
    # Search can detect one protocol, then fail a later ADC/BigBuf probe with
    # ESOFT (-10), which also denotes an ordinary empty search. Native callers
    # may even return zero after a failed download. Neither result may publish
    # an earlier UID or rearm presence tracking. A card protocol CRC check is
    # different from an invalid native serial packet/frame.
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)
    return code in {-2, -3, -4, -5, -8} or bool(re.search(
        r'UART\s+CreateFile\s+failed|invalid serial port|cannot communicate with.*Proxmark|'
        r'No response from Proxmark3|communication error|resynchronising|'
        r'command execution time\s*out|connection (?:lost|closed)|'
        r'received packet[^\r\n]*(?:invalid\s+CRC\b|too short|without postamble)|'
        r'\bBigBuf download\s+(?:cancelled|failed|aborted|outside device memory)\b|'
        r'\b(?:invalid or incomplete|failed to allocate|failed to send)\s+BigBuf download\b|'
        r'\bdevice disconnected during BigBuf download\b|'
        r'\bTimed out while trying to download data from device\b|'
        r'\bsample download\s+(?:failed|cancelled|aborted)\b|'
        r'\bcapture\s+(?:failed|cancelled|aborted)\b|'
        r'\bincomplete Texkom ADC capture\b|\bGet samples error\b|'
        r'\b(?:failed to read sample format|invalid sample data or format);\s*graph buffer cleared\b',
        text, re.I))


def scan_card(band, output):
    """Parse a single search reply, never progress labels or accumulated logs.

    None means explicit absence. Unrecognised replies never rearm deduplication.
    Unsupported profiles retain detection evidence, not an invented memory dump.
    """
    if band not in {'hf', 'lf'}:
        raise ValueError('Invalid intake band.')
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output).replace('\r', '\n')
    if re.search(r'\bmultiple (?:cards|tags)\b|more than one (?:card|tag)', text, re.I):
        raise MultipleCards('Leave one card on the antenna.')
    markers = re.findall(r'^\s*(?:\[\+\]\s*)?Valid\s+(.+?)\s+found!?\s*$', text, re.I | re.M)
    if band == 'lf':
        aliases = [('EM410x', r'EM\s*410x'), ('HID Prox', r'HID\s+Prox'), ('Indala', r'Indala'),
                   ('AWID', r'AWID'), ('ioProx', r'IO\s*Prox'), ('FDX-B', r'FDX[- ]B')]
        profiles = [name for name, pattern in aliases if any(re.search(pattern, m, re.I) for m in markers)]
        if not markers and re.search(r'\bEM\s*410x\s+ID\s*:', text, re.I):
            profiles = ['EM410x']
        if len(markers) > 1 or len(profiles) > 1:
            raise MultipleCards('Several LF profiles were detected.')
        if profiles:
            try:
                fields = lf_identity(profiles[0], text)
            except ValueError as exc:
                raise UnclearScan(str(exc)) from exc
            return IntakeCard('lf', profiles[0], fields['identifier'], 'em410x' if profiles[0] == 'EM410x' else 'lf_profile')
        if markers:
            return IntakeCard('lf', markers[0], kind='detection')
    else:
        if len(markers) > 1:
            raise MultipleCards('Several HF profiles were detected.')
        uids = {re.sub('[ :\t]', '', m).upper() for m in re.findall(
            r'\b(?:UID|IDm|CSN)\)?\s*[.:]+\s*((?:[0-9A-F]{2}[ :\t]*){4,10})(?![0-9A-F])', text, re.I)}
        if len(uids) > 1:
            raise MultipleCards('Several HF identifiers were detected.')
        uid = next(iter(uids), '')
        if markers or uid:
            # Protocol hints are inspected only after actual detection, not while
            # the client prints "Searching for FeliCa/LEGIC/..." progress.
            marker = markers[0] if markers else ''
            for name, pattern in [('ISO15693 / NFC-V', r'ISO\s*15693'), ('FeliCa', r'FeliCa'),
                                  ('iCLASS / PicoPass', r'iCLASS|PicoPass'), ('LEGIC', r'LEGIC'), ('Topaz', r'Topaz')]:
                if re.search(pattern, marker, re.I):
                    return IntakeCard('hf', name, uid, 'hf_profile')
            for name, pattern in [('DESFire', r'\bDESFire\b'), ('MIFARE Plus', r'\bMIFARE\s+Plus\b')]:
                if re.search(pattern, text, re.I):
                    return IntakeCard('hf', name, uid, 'hf_profile')
            # The existing 14A parser deliberately supports only 4/7/10-byte UID.
            if len(uid) in {8, 14, 20}:
                detected = parse_detection(text)
                if detected.ambiguous_identity:
                    raise MultipleCards(detected.reason)
                if detected.family in {'mfc', 'mfu'} and detected.supported:
                    return IntakeCard('hf', detected.protocol, detected.uid, detected.family, detected.classic_size)
            return IntakeCard('hf', marker or 'ISO14443-A', uid)
    if re.search(r'No known(?:/supported)?[^\n]*tags found|No (?:card|tag) response|No (?:cards|tags) found', text, re.I):
        return None
    raise UnclearScan('Search did not return a confirmed card or an explicit empty result.')
