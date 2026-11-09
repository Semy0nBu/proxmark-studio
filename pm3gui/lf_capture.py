"""Positive proof for finite LF GraphBuffer captures before offline decoding."""
import re
from .operations import _text
from .auto_capture import transport_failed
from .studio_data import say


def validate_lf_search_capture(output, code=0, expected_samples=30000):
    """Reject incomplete/corrupt captures even when native lf read returns zero.

    This client ignores getSamples()'s error in lf_read_internal(). Data fetched
    is printed only after the requested memory transfer completes; the sample
    format/unpack evidence is also required before trusting a new GraphBuffer.
    """
    text=_text(output)
    corrupt=re.search(
        r'\b(?:timeout|failed|error|aborted|partial|insufficient)\b|timed?\s*out|'
        r'CRC[^\n]*(?:<>|!=|mismatch|fail|invalid|wrong|bad)|'
        r'(?:invalid|wrong|bad|failed)[^\n]*\bCRC\b|'
        r'\bdropped?\s+\d+\s+bytes\b|\bOLD\b[^\n]*(?:short|\d+\s*/\s*\d+)|'
        r'(?:packet|payload|preamble|postamble)[^\n]*\bshort\b|resynchroni[sz]',text,re.I)
    if code or transport_failed(text,code) or corrupt:
        raise ValueError(say('Захват LF прерван или повреждён. Старый GraphBuffer не разбирается.',
                             'LF capture failed or was corrupted. The previous GraphBuffer will not be decoded.'))
    prefix=r'^\s*(?:\[[^\]\n]+\]\s*)?'
    sizes={int(value) for value in re.findall(prefix+r'Reading\s+(\d+)\s+bytes from device memory\s*$',text,re.I|re.M)}
    formats={(int(bits),int(dec)) for bits,dec in re.findall(
        prefix+r'Samples\s*@\s*(\d+)\s+bits/smpl,\s*decimation\s+1:(\d+)\s*$',text,re.I|re.M)}
    fetched=re.search(prefix+r'Data fetched\s*$',text,re.I|re.M)
    if sizes!={expected_samples} or not fetched or len(formats)!=1:
        raise ValueError(say('Нет полного подтверждения нового захвата LF. Разбор старых данных запрещён.',
                             'The new LF capture was not fully confirmed. Decoding stale data is blocked.'))
    bits,decimation=formats.pop()
    if not 1<=bits<=8 or not 1<=decimation<=8:
        raise ValueError(say('Не подтверждён формат отсчётов LF.','The LF sample format was not confirmed.'))
    if bits<8 and {int(n) for n in re.findall(prefix+r'Unpacked\s+(\d+)\s+samples\s*$',text,re.I|re.M)}!={expected_samples}:
        raise ValueError(say('Распаковка отсчётов LF не завершена.','LF sample unpacking did not complete.'))
    return {'samples':expected_samples,'bits_per_sample':bits,'decimation':decimation}
