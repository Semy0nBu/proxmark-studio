"""Classify existing records by protocol evidence, never by UID or byte count."""
import re

HF_TYPES={'mfcard','mfc v2','mfc v3','mfu','fudan','iclass','legic v2','topaz','14b v2','lto','cryptorf','ndef'}
LF_TYPES={'hitag','hitag1','hitag2','hitags','hitagu','t55x7','t5555','em4205/em4305','em4469/em4569','em4x50'}


def card_band(card,metadata=None):
    metadata=metadata if isinstance(metadata,dict) else {}
    explicit=metadata.get('rf_band')
    if explicit in ('hf','lf'):return explicit
    validation=metadata.get('read_validation');kind=validation.get('kind') if isinstance(validation,dict) else None
    if kind in ('mfc','mfu'):return 'hf'
    if kind=='em410x':return 'lf'
    source=metadata.get('source_json');source=source if isinstance(source,dict) else {}
    ftype=str(source.get('FileType','')).lower()
    if ftype in HF_TYPES or ftype.startswith('15693'):return 'hf'
    if ftype in LF_TYPES:return 'lf'
    protocol=' '.join((str(card.get('protocol','')),str(metadata.get('protocol','')))).casefold()
    if re.search(r'\b(?:mifare|classic|ntag\w*|ultralight|desfire|iclass|picopass|felica|fudan|legic|topaz|iso\s*[-/]?\s*(?:14443\w*|15693|18092)|nfc|lto|cryptorf|ndef)\b',protocol):return 'hf'
    if re.search(r'\b(?:em\s*4(?:10\w*|2\w*|3\w*|4\w*|5\w*|x\w*)|t55(?:xx|x7|[0-9]{2})|hitag\w*|hid(?:\s+prox)?|indala|awid|farpointe?|io\s*prox|pyramid|nedap|viking|nexwatch|pac|presco|noralsy|jablotron|keri|gproxii)\b',protocol):return 'lf'
    return 'unknown'
