"""Read-only projections and printable previews; never modify source bytes."""
from .i18n import tr
import codecs
from .operations import CLASSIC_GEOMETRIES,sector_trailer


def without_classic_keys(data,*,block_size=16,kind=None,filetype=None):
    if block_size!=16 or kind not in (None,'mfc') or filetype not in (None,'raw','mfcard','mfc v2','mfc v3'):
        raise ValueError(tr('Режим «Без ключей MF» предназначен для полного образа MIFARE Classic.'))
    positions,geometry=classic_view_positions(len(data))
    return bytes(data[i] for i in positions),geometry


def classic_view_positions(size):
    geometry=next((sectors for sectors,length in CLASSIC_GEOMETRIES.values() if length==size),None)
    if geometry is None:
        raise ValueError(tr('Нужен образ Classic Mini / 1K / 2K / 4K: 320, 1024, 2048 или 4096 байт.'))
    # Keep ACL/GPB (middle four bytes), UID and all data bytes in source order.
    hidden={offset for sector in range(geometry) for start in (sector_trailer(sector)*16,sector_trailer(sector)*16+10)
            for offset in range(start,start+6)}
    return tuple(offset for offset in range(size) if offset not in hidden),geometry


def _glyphs(data,encoding,positions):
    """Decode contiguous visible spans, preserving original byte addresses.

    UTF-8/UTF-16 characters may cross display rows. Hidden fields always end a
    span so removing keys cannot create a new multibyte character or string.
    Invalid bytes have an explicit printable marker only in these previews.
    """
    name=codecs.lookup(encoding).name
    cursor=0
    while cursor<len(positions):
        start=positions[cursor];end=cursor+1
        while end<len(positions) and positions[end]==positions[end-1]+1:end+=1
        stop=positions[end-1]+1;offset=start
        while offset<stop:
            width=1
            if name=='utf-8':
                lead=data[offset];width=2 if 0xc2<=lead<=0xdf else 3 if 0xe0<=lead<=0xef else 4 if 0xf0<=lead<=0xf4 else 1
            elif name in {'utf-16-le','utf-16-be'}:
                width=2
                unit=int.from_bytes(data[offset:offset+2],'little' if name.endswith('le') else 'big')
                if 0xd800<=unit<=0xdbff and offset+4<=stop:
                    following=int.from_bytes(data[offset+2:offset+4],'little' if name.endswith('le') else 'big')
                    if 0xdc00<=following<=0xdfff:width=4
            width=min(width,stop-offset)
            try:character=data[offset:offset+width].decode(encoding,errors='strict');valid=character.isprintable()
            except UnicodeError:
                character='·';valid=False
                if name=='utf-8':width=1
            yield offset,width,character if valid else '·',valid
            offset+=width
        cursor=end


def readable_preview(data,encoding,*,positions=None,strings_only=False):
    """Return (read-only text, number of rows/strings); offsets are original."""
    positions=tuple(range(len(data))) if positions is None else tuple(p for p in positions if p<len(data))
    glyphs=_glyphs(data,encoding,positions)
    if strings_only:
        lines=[];run=[];previous=None
        def flush():
            first,last=0,len(run)
            while first<last and run[first][1].isspace():first+=1
            while last>first and run[last-1][1].isspace():last-=1
            value=''.join(c for _,c in run[first:last])
            if len(value)>=4 and any(c.isalnum() for c in value):lines.append(f'{run[first][0]:04X}  {value}')
            run.clear()
        for offset,width,char,valid in glyphs:
            if previous!=offset or not valid:flush()
            if valid:run.append((offset,char))
            previous=offset+width
        flush();return '\n'.join(lines),len(lines)
    rows={}
    for offset,width,char,valid in glyphs:
        row=offset//16;cells=rows.setdefault(row,[' ']*16);cells[offset%16]=char
    lines=[];zero_rows=[]
    visible=set(positions)
    def flush_zeros():
        if not zero_rows:return
        start,end=zero_rows[0]*16,min(len(data),zero_rows[-1]*16+16)-1
        lines.append(tr('{0}–{1}  [нулевые блоки: {2}]', f'{start:04X}', f'{end:04X}', f'{len(zero_rows)}') if len(zero_rows)>1 else
                     f'{start:04X}  '+''.join(rows[zero_rows[0]]))
        zero_rows.clear()
    for row,cells in sorted(rows.items()):
        offsets=[i for i in range(row*16,min(len(data),(row+1)*16)) if i in visible]
        if offsets and all(data[i]==0 for i in offsets):zero_rows.append(row);continue
        flush_zeros();lines.append(f'{row*16:04X}  '+''.join(cells).rstrip())
    flush_zeros();return '\n'.join(lines),len(lines)
