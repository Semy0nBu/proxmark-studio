"""Bounded flash transcript and phase-aware diagnostics; no device access."""
import codecs
from dataclasses import dataclass
import re
from .device import _StreamCleaner

_FAILURE = re.compile(r'\b(?:error|failed|failure|timeout|timed out|aborted|nack)\b', re.I)
_OPEN = re.compile(r'^\s*(?:\[[^\]\n]+\]\s*)?UART\s+CreateFile\s+failed\b[^\n]*\(Win32 error (?:2|1168|121)\)\s*$', re.I)
_INVALID_PORT = re.compile(r'^\s*\[!+\]\s*ERROR:\s*invalid serial port COM[1-9][0-9]*\s*$', re.I)


@dataclass(frozen=True)
class FlashDiagnostics:
    all_done: bool
    fatal_lines: tuple[str, ...]
    reconnect_lines: int


class FlashTranscript:
    def __init__(self, limit=4*1024*1024, *, allow_transition=True):
        if type(limit) is not int or limit<512:raise ValueError('A bounded log capacity of at least 512 bytes is required.')
        self.limit = limit
        self.head_limit = min(65536, limit//4)
        self.head = bytearray();self.tail = bytearray()
        self.total_bytes = 0
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.cleaner = _StreamCleaner()
        self.line = ''
        self.transition = self.writing = self.all_done = False
        self.fatal = []
        self.reconnect_lines = 0
        self.allow_transition=allow_transition

    def feed(self, data):
        self.total_bytes += len(data)
        remaining = self.head_limit-len(self.head)
        self.head.extend(data[:max(0, remaining)])
        self.tail.extend(data[max(0, remaining):])
        tail_limit = self.limit-self.head_limit
        if len(self.tail)>tail_limit:del self.tail[:-tail_limit]
        # Terminal redraw controls must not join diagnostic words together.
        parts=(self.line+self.cleaner.feed(self.decoder.decode(data).replace('\r','\n').replace('\b','\n'))).split('\n')
        self.line=parts.pop()
        for line in parts:self._line(line)
        while len(self.line)>8192:
            self._line(self.line[:8192]);self.line=self.line[8064:]

    def _line(self, line):
        if not line.strip():return
        if re.search(r'\bEntering bootloader\b|\bTrigger restart\b',line,re.I):self.transition=True
        if re.search(r'\bWriting segments for file\b|\bWriting flash\b',line,re.I):self.writing=True
        if re.search(r'\bAll done\b',line,re.I):self.all_done=True
        if not _FAILURE.search(line):return
        transient = self.allow_transition and self.transition and not self.writing and not self.all_done and (_OPEN.fullmatch(line) or _INVALID_PORT.fullmatch(line))
        if transient:self.reconnect_lines+=1
        elif len(self.fatal)<32 and line[:512] not in self.fatal:self.fatal.append(line[:512])

    def finish(self):
        self.line += self.cleaner.feed(self.decoder.decode(b'',final=True))
        if self.line:self._line(self.line)
        self.line=''
        return FlashDiagnostics(self.all_done,tuple(self.fatal),self.reconnect_lines)

    @property
    def truncated(self):return self.total_bytes>self.limit

    def retained(self):
        marker=b'\n[GUI: middle of oversized client log omitted; process was not interrupted]\n' if self.truncated else b''
        return bytes(self.head)+marker+bytes(self.tail)


def flash_diagnostics(output):
    transcript=FlashTranscript()
    transcript.feed(output.encode('utf-8','replace'))
    return transcript.finish()
