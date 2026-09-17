#!/usr/bin/env python3
# Copyright (c) 2026 Dr Daniel Mompel Riera
# Licensed under the GNU Affero General Public License v3.0.
# Free to use and change; if you pass on a changed version, or let anyone
# use it over a network, you must publish your source under the same licence.
# Commercial use needs my permission: dmompelriera@nlcsjeju.kr
"""Build a birthday profile from the two school exports.

    import.py <main.xlsx> <StudentIDBadge.xls|.xlsx|-> <out_dir> [initials] [extra.xlsx ...]

Extra spreadsheets are matched to the main one on surname + forename + date of
birth, and only fill in what the main one is missing. That is how the Export
Wizard file adds preferred names without losing the year group, form, house and
tutor initials that only the Student Report carries.

Pure Python standard library - no pip, no Homebrew. Photos are resized with
`sips`, which ships with macOS; if it is missing the originals are kept.

Writes <out_dir>/students.csv and <out_dir>/photos/NNN.jpg, and prints a
one-line-per-fact report on stdout for the calling script to show.
"""
import sys, os, re, csv, collections, zipfile, struct, subprocess, shutil
import xml.etree.ElementTree as ET

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


# --------------------------------------------------------------- .xlsx reading
def _shared_strings(z):
    try: raw = z.read('xl/sharedStrings.xml')
    except KeyError: return []
    out = []
    for si in ET.fromstring(raw).findall(NS + 'si'):
        out.append(''.join(t.text or '' for t in si.iter(NS + 't')))
    return out


def read_xlsx(path):
    """-> list of rows, each a list of cell strings (blank-padded)."""
    with zipfile.ZipFile(path) as z:
        sst = _shared_strings(z)
        name = next((n for n in z.namelist()
                     if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)), None)
        if not name:
            raise ValueError('no worksheet found in %s' % os.path.basename(path))
        rows = []
        for r in ET.fromstring(z.read(name)).iter(NS + 'row'):
            cells = {}
            for c in r.findall(NS + 'c'):
                ref = c.get('r') or ''
                col = re.match(r'[A-Z]+', ref)
                if not col: continue
                idx = 0
                for ch in col.group(0): idx = idx * 26 + (ord(ch) - 64)
                t, v = c.get('t'), c.find(NS + 'v')
                if t == 's' and v is not None and v.text is not None:
                    val = sst[int(v.text)] if int(v.text) < len(sst) else ''
                elif t == 'inlineStr':
                    val = ''.join(x.text or '' for x in c.iter(NS + 't'))
                else:
                    val = (v.text or '') if v is not None else ''
                cells[idx - 1] = val
            rows.append([cells.get(i, '') for i in range(max(cells) + 1)] if cells else [])
        return rows


# ------------------------------------------------- legacy .xls (OLE + BIFF8)
def ole_stream(path, want='Workbook'):
    """Minimal Compound File reader - enough to pull one large stream out."""
    d = open(path, 'rb').read()
    if d[:8] != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return None
    ssz = 1 << struct.unpack_from('<H', d, 30)[0]
    n_difat = struct.unpack_from('<I', d, 72)[0]
    first_difat = struct.unpack_from('<I', d, 68)[0]
    sect = lambda i: d[512 + i * ssz: 512 + (i + 1) * ssz]

    difat = list(struct.unpack_from('<109I', d, 76))
    nxt, guard = first_difat, 0
    while nxt not in (0xFFFFFFFE, 0xFFFFFFFF) and guard < n_difat + 8:
        blk = sect(nxt)
        difat += list(struct.unpack_from('<%dI' % (ssz // 4 - 1), blk, 0))
        nxt = struct.unpack_from('<I', blk, ssz - 4)[0]; guard += 1

    fat = []
    for s in difat:
        if s in (0xFFFFFFFE, 0xFFFFFFFF): continue
        fat += list(struct.unpack_from('<%dI' % (ssz // 4), sect(s), 0))

    def chain(start, size):
        parts, cur, guard = [], start, 0        # list+join: concatenating 37k sectors is O(n^2)
        while cur not in (0xFFFFFFFE, 0xFFFFFFFF) and guard < len(fat) + 8:
            parts.append(sect(cur))
            cur = fat[cur] if cur < len(fat) else 0xFFFFFFFE
            guard += 1
        out = b''.join(parts)
        return out[:size] if size else out

    dir_sect = struct.unpack_from('<I', d, 48)[0]
    ents = chain(dir_sect, 0)
    for off in range(0, len(ents) - 127, 128):
        nlen = struct.unpack_from('<H', ents, off + 64)[0]
        if not 0 < nlen <= 64: continue
        nm = ents[off:off + nlen - 2].decode('utf-16-le', 'ignore')
        if nm == want:
            start = struct.unpack_from('<I', ents, off + 116)[0]
            size = struct.unpack_from('<I', ents, off + 120)[0]
            return chain(start, size)
    return None


def biff_records(data):
    p = 0
    while p + 4 <= len(data):
        rid, ln = struct.unpack_from('<HH', data, p)
        yield rid, data[p + 4:p + 4 + ln]
        p += 4 + ln


def _join(data, first_id):
    """Concatenate a record with the CONTINUE records that follow it."""
    out, cur = [], None
    for rid, body in biff_records(data):
        if rid == first_id:
            if cur is not None: out.append(b''.join(cur))
            cur = [body]
        elif rid == 0x003C and cur is not None:
            cur.append(body)
        elif cur is not None:
            out.append(b''.join(cur)); cur = None
    if cur is not None: out.append(b''.join(cur))
    return out


def _sst_strings(data):
    """Shared string table. Handles CONTINUE splits and the 8/16-bit flag."""
    chunks, cur, parts = [], None, []
    for rid, body in biff_records(data):
        if rid == 0x00FC:
            cur = [body]; parts = []
        elif rid == 0x003C and cur is not None:
            parts.append(len(b''.join(cur))); cur.append(body)
        elif cur is not None:
            chunks.append((b''.join(cur), parts)); cur = None
    if cur is not None: chunks.append((b''.join(cur), parts))
    if not chunks: return []
    blob, breaks = chunks[0]
    breaks = set(breaks)

    strings, p = [], 8
    n = struct.unpack_from('<I', blob, 4)[0]
    while len(strings) < n and p + 3 <= len(blob):
        cch = struct.unpack_from('<H', blob, p)[0]; p += 2
        grbit = blob[p]; p += 1
        rich = cRun = 0; ext = 0
        if grbit & 0x08: cRun = struct.unpack_from('<H', blob, p)[0]; p += 2
        if grbit & 0x04: ext = struct.unpack_from('<I', blob, p)[0]; p += 4
        wide = grbit & 0x01
        buf, need = [], cch
        while need > 0:
            avail = len(blob) - p
            take = need
            # a CONTINUE boundary restarts the encoding with a fresh flag byte
            nb = min([b for b in breaks if b > p], default=None)
            room = (nb - p) if nb is not None else avail
            take = min(need, max(0, room // (2 if wide else 1)))
            if take:
                seg = blob[p:p + take * (2 if wide else 1)]
                buf.append(seg.decode('utf-16-le' if wide else 'latin-1', 'ignore'))
                p += take * (2 if wide else 1); need -= take
            if need > 0:
                if nb is None or nb >= len(blob): break
                p = nb; wide = blob[p] & 0x01; p += 1
                breaks.discard(nb)
        p += cRun * 4 + ext
        strings.append(''.join(buf))
    return strings


def read_xls_cells(data):
    """-> {(row, col): text} for shared-string cells."""
    sst = _sst_strings(data)
    cells = {}
    for rid, body in biff_records(data):
        if rid == 0x00FD and len(body) >= 10:                      # LABELSST
            r, c, _x, isst = struct.unpack_from('<HHHI', body, 0)
            if isst < len(sst): cells[(r, c)] = sst[isst]
        elif rid == 0x0204 and len(body) >= 8:                     # LABEL
            r, c = struct.unpack_from('<HH', body, 0)
            cch = struct.unpack_from('<H', body, 6)[0]
            try:
                wide = body[8] & 0x01
                s = body[9:9 + cch * (2 if wide else 1)]
                cells[(r, c)] = s.decode('utf-16-le' if wide else 'latin-1', 'ignore')
            except Exception:
                pass
    return cells


BLIP = {0xF01A: 'emf', 0xF01B: 'wmf', 0xF01C: 'pict', 0xF01D: 'jpg',
        0xF01E: 'png', 0xF01F: 'dib', 0xF029: 'tiff', 0xF02A: 'jpg'}


def xls_pictures(data):
    """-> ([(ext, bytes)] in blip order, [(blip_index, anchor_row)])"""
    grp = _join(data, 0x00EB)
    blips = []
    if grp:
        esc = grp[0]
        off, store = 8, None
        while off + 8 <= len(esc):
            vi, rt, sz = struct.unpack_from('<HHI', esc, off)
            if rt == 0xF001: store = (off + 8, off + 8 + sz); break
            off += 8 + sz
        if store:
            p, end = store
            while p + 8 <= end:
                vi, rt, sz = struct.unpack_from('<HHI', esc, p)
                if rt != 0xF007: break
                d0 = p + 8
                b = d0 + 36 + esc[d0 + 34]                       # skip BSE header + name
                bvi, brt, bsz = struct.unpack_from('<HHI', esc, b)
                if brt not in BLIP: break
                q = b + 8 + 16
                if (bvi >> 4) in (0x46B, 0x6E1, 0x7A9, 0x3D5, 0x217): q += 16
                if brt in (0xF01D, 0xF01E, 0xF01F, 0xF029, 0xF02A): q += 1
                blips.append((BLIP[brt], esc[q:b + 8 + bsz]))
                p = b + 8 + bsz            # the BSE length field is not trustworthy here

    sheet = b''.join(_join(data, 0x00EC))
    shapes, state = [], {}

    def walk(buf, off, end):
        while off + 8 <= end:
            vi, rt, sz = struct.unpack_from('<HHI', buf, off)
            ver, inst = vi & 0xF, vi >> 4
            ds, de = off + 8, min(off + 8 + sz, end)
            if rt == 0xF004:                                     # shape container
                state.clear(); walk(buf, ds, de)
                if 'pib' in state and 'row' in state:
                    shapes.append((state['pib'], state['row']))
            elif rt == 0xF00B:                                   # shape properties
                p = ds
                for _ in range(inst):
                    if p + 6 > de: break
                    pid, val = struct.unpack_from('<HI', buf, p); p += 6
                    if (pid & 0x3FFF) == 0x0104: state['pib'] = val
            elif rt == 0xF010 and de - ds >= 18:                 # client anchor
                state['row'] = struct.unpack_from('<H', buf, ds + 6)[0]
            elif ver == 0xF:
                walk(buf, ds, de)
            off = de
    walk(sheet, 0, len(sheet))
    seen, uniq = set(), []
    for pib, row in shapes:
        if pib not in seen: seen.add(pib); uniq.append((pib, row))
    uniq.sort(key=lambda s: s[1])
    return blips, uniq


def xlsx_pictures(path):
    """Badge file exported as .xlsx instead: images live in xl/media."""
    with zipfile.ZipFile(path) as z:
        media = sorted(n for n in z.namelist() if n.startswith('xl/media/'))
        return [(n.rsplit('.', 1)[-1].lower(), z.read(n)) for n in media], []


# --------------------------------------------------------------- field mapping
def norm(s): return re.sub(r'[^a-z]', '', str(s or '').lower())
def digits(v):
    """'Year 10' -> '10', 10.0 -> '10', '' -> ''. Enrolment Year arrives as a number."""
    m = re.search(r'\d+', str(v if v is not None else '').replace('.0', ''))
    return m.group(0) if m else ''


def clean_pref(pref, forename):
    """A preferred name identical to the forename is not a preferred name."""
    p = str(pref or '').strip()
    return '' if not p or norm(p) == norm(forename) else p

WANT = {
    'surname':   ('surname', 'lastname', 'familyname'),
    'forename':  ('forename', 'firstname', 'givenname'),
    'preferred': ('preferredname', 'knownas', 'chosenname', 'preferredforename', 'nickname'),
    'dob':       ('dateofbirth', 'dob', 'birthdate'),
    'year':      ('yeargroup', 'year', 'nc year', 'ncyear'),
    'form':      ('form', 'formgroup', 'tutorgroup', 'registrationgroup'),
    'house':     ('academichouse', 'house'),
    'tutorinit': ('tutorinitials', 'tutorinits', 'tutorcode'),
    'gender':    ('gender', 'sex'),
    # The Students Simple Report carries these two and nothing used to read them.
    # "Enrolment Year" is the year group the student joined in, as a bare number, so
    # enrolled == year group means this is their first year in the school.
    'enrolled':  ('enrolmentyear', 'enrollmentyear', 'yearofentry', 'entryyear',
                  'yearjoined', 'joiningyear', 'yearofadmission', 'admissionyear'),
    'status':    ('schoolstatus', 'boardingstatus', 'daystatus', 'boarderday'),
}


def find_header(rows):
    for i, r in enumerate(rows[:12]):
        keys = {norm(c) for c in r}
        if norm('Surname') in keys and (norm('Forename') in keys or 'firstname' in keys):
            cols = {}
            for want, names in WANT.items():
                for j, c in enumerate(r):
                    if norm(c) in {norm(n) for n in names}:
                        cols[want] = j; break
            return i, cols
    raise ValueError('could not find a header row with Surname and Forename')


DATE = re.compile(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})')


def parse_dob(v):
    s = str(v or '').strip()
    m = DATE.search(s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        if d > 12 and mo <= 12: pass
        elif mo > 12 and d <= 12: d, mo = mo, d          # tolerate MM/DD/YYYY
        if 1 <= d <= 31 and 1 <= mo <= 12: return d, mo, y
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m: return int(m.group(3)), int(m.group(2)), int(m.group(1))
    if re.fullmatch(r'\d+(\.\d+)?', s):                  # Excel serial date
        import datetime
        try:
            dt = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(float(s)))
            return dt.day, dt.month, dt.year
        except Exception:
            pass
    return None


# --------------------------------------------------------------------- main
def list_initials(report):
    rows = read_xlsx(report)
    hi, cols = find_header(rows)
    if 'tutorinit' not in cols:
        return []
    from collections import Counter
    c = Counter()
    for r in rows[hi + 1:]:
        if 'tutorinit' in cols and cols['tutorinit'] < len(r):
            v = str(r[cols['tutorinit']]).strip().upper()
            if v: c[v] += 1
    return c.most_common()


def load_info(path, initials=''):
    """Read any spreadsheet that has surname / forename / date of birth."""
    rows = read_xlsx(path)
    hi, cols = find_header(rows)
    get = lambda r, k: (r[cols[k]] if k in cols and cols[k] < len(r) else '')
    out, skipped = [], 0
    for r in rows[hi + 1:]:
        if not r or not str(get(r, 'surname')).strip(): continue
        if str(get(r, 'surname')).strip().lower() in ('total', 'count'): continue
        dob = parse_dob(get(r, 'dob'))
        if not dob: skipped += 1; continue
        d, mo, y = dob
        fore = str(get(r, 'forename')).strip()
        out.append({
            'forename': fore, 'surname': str(get(r, 'surname')).strip(),
            'preferred_name': clean_pref(get(r, 'preferred'), fore),
            'day': d, 'month': mo, 'birth_year': y,
            'year_group': str(get(r, 'year')).strip(),
            'form': str(get(r, 'form')).strip(),
            'house': str(get(r, 'house')).strip(),
            'gender': str(get(r, 'gender')).strip()[:1].upper(),
            'is_tutee': 'yes' if initials and str(get(r, 'tutorinit')).strip().upper() == initials else '',
            'enrolled_year': digits(get(r, 'enrolled')),
            'school_status': str(get(r, 'status')).strip(),
            'photo': '',
        })
    return out, cols, skipped


def rec_key(s):
    return (norm(s['surname']), norm(s['forename']), s['day'], s['month'], s['birth_year'])


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == '--count':
        try:
            recs, _c, _s = load_info(sys.argv[2])
            print(len(recs))
        except Exception as e:
            print('ERROR %s' % e, file=sys.stderr); return 2
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == '--initials':
        try:
            for code, n in list_initials(sys.argv[2]):
                print('%s\t%d' % (code, n))
        except Exception as e:
            print('ERROR %s' % e, file=sys.stderr); return 2
        return 0
    if len(sys.argv) < 4:
        print('ERROR usage: import.py <main> <badges|-> <out_dir> [initials] [extra ...]'); return 2
    report, badges, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    initials = (sys.argv[4] if len(sys.argv) > 4 else '').strip().upper()

    students, cols, skipped = load_info(report, initials)
    for req in ('surname', 'forename', 'dob'):
        if req not in cols:
            print('ERROR the main spreadsheet has no %s column' % req); return 2
    print('INFO students read: %d' % len(students))
    if skipped: print('WARN rows skipped for an unreadable date of birth: %d' % skipped)
    for f, label in (('year_group', 'year group'), ('form', 'form'), ('house', 'house'),
                     ('enrolled_year', 'enrolment year'), ('school_status', 'school status')):
        if not any(s[f] for s in students):
            print('WARN the main spreadsheet has no %s column' % label)
    newcount = sum(1 for s in students
                   if s['enrolled_year'] and digits(s['year_group']) == s['enrolled_year'])
    if any(s['enrolled_year'] for s in students):
        print('INFO new this year (enrolled in their current year group): %d' % newcount)

    # ---------------------------- merge any extra spreadsheets (the Export Wizard file)
    for extra in sys.argv[5:]:
        if not extra or extra == '-' or not os.path.exists(extra): continue
        name = os.path.basename(extra)
        try:
            more, _mc, _sk = load_info(extra, initials)
        except Exception as e:
            print('WARN could not read %s: %s' % (name, str(e)[:110])); continue
        by_key = {rec_key(r): r for r in more}
        by_name = {}
        for r in more: by_name.setdefault((norm(r['surname']), norm(r['forename'])), r)
        got_pref = filled = unmatched = 0
        for st in students:
            other = by_key.get(rec_key(st)) or by_name.get((norm(st['surname']), norm(st['forename'])))
            if not other: unmatched += 1; continue
            if other['preferred_name'] and not st['preferred_name']:
                st['preferred_name'] = other['preferred_name']; got_pref += 1
            for f in ('year_group', 'form', 'house', 'gender',
                      'enrolled_year', 'school_status'):
                if not st[f] and other.get(f): st[f] = other[f]; filled += 1
            if not st['is_tutee'] and other['is_tutee']: st['is_tutee'] = other['is_tutee']
        print('INFO %s: matched %d of %d students' % (name, len(students) - unmatched, len(students)))
        if got_pref: print('INFO   preferred names taken from it: %d' % got_pref)
        if filled:   print('INFO   blank fields filled in: %d' % filled)
        if unmatched: print('WARN   students it had nothing for: %d' % unmatched)

    # --------------------------------------------------------------- photos
    photo_dir = os.path.join(out_dir, 'photos')
    matched = 0
    if badges and badges != '-' and os.path.exists(badges):
        os.makedirs(photo_dir, exist_ok=True)
        for f in os.listdir(photo_dir):
            if f.lower().endswith(('.jpg', '.jpeg', '.png')): os.remove(os.path.join(photo_dir, f))

        pics, anchors, cells = [], [], {}
        if zipfile.is_zipfile(badges):
            pics, anchors = xlsx_pictures(badges)
        else:
            data = ole_stream(badges)
            if data:
                pics, anchors = xls_pictures(data)
                cells = read_xls_cells(data)
        print('INFO photos found in the badge file: %d' % len(pics))

        # badge text -> the row it sits on, so a photo can be tied to a name
        by_order = []
        for (r, c), txt in sorted(cells.items()):
            if 'ID:' not in txt and 'Year:' not in txt: continue
            lines = [l.strip() for l in str(txt).split('\n') if l.strip()]
            if len(lines) < 2: continue
            info = {'row': r, 'forename': lines[0], 'surname': lines[1]}
            for l in lines:
                m = re.match(r'(Year|Form|House|ID)\s*:\s*(.+)', l)
                if m: info[m.group(1).lower()] = m.group(2).strip()
            by_order.append(info)
        by_order.sort(key=lambda b: b['row'])

        pairs = []
        if by_order and anchors and len(by_order) == len(anchors):
            for info, (pib, arow) in zip(by_order, anchors):
                pairs.append((pib, info))                       # both lists are row-ordered
        elif by_order and len(by_order) == len(pics):
            for i, info in enumerate(by_order): pairs.append((i + 1, info))

        # A badge belongs to exactly one student, so hold them as queues and take from the
        # most specific key that matches. Two students really can share a name - there are
        # two Ian Kims - and without this they would end up sharing a face as well.
        ynorm = lambda v: re.sub(r'\D', '', str(v or ''))
        by_form, by_year, by_name = {}, {}, {}
        for pib, info in pairs:
            sn, fn = norm(info['surname']), norm(info['forename'])
            by_form.setdefault((sn, fn, norm(info.get('form', ''))), []).append(pib)
            by_year.setdefault((sn, fn, ynorm(info.get('year', ''))), []).append(pib)
            by_name.setdefault((sn, fn), []).append(pib)
        used, guessed = set(), []
        namecount = collections.Counter((norm(s['surname']), norm(s['forename'])) for s in students)

        def take(q):
            while q:
                p = q.pop(0)
                if p not in used and 1 <= p <= len(pics):
                    used.add(p); return p
            return None

        def save(pib, slug):
            ext, blob = pics[pib - 1]
            dst = os.path.join(photo_dir, slug + '.jpg')
            tmp = dst + '.orig'
            open(tmp, 'wb').write(blob)
            if shutil.which('sips') and ext in ('jpg', 'png'):
                ok = subprocess.run(['sips', '-Z', '480', tmp, '--out', dst],
                                    capture_output=True).returncode == 0
                os.remove(tmp) if ok else os.rename(tmp, dst)
            else:
                os.rename(tmp, dst)

        for s in students:
            sn, fn = norm(s['surname']), norm(s['forename'])
            pib = None
            if s.get('form'):
                pib = take(by_form.get((sn, fn, norm(s['form'])), []))
            if not pib and s.get('year_group'):
                pib = take(by_year.get((sn, fn, ynorm(s['year_group'])), []))
            if not pib:
                if namecount[(sn, fn)] > 1:
                    guessed.append('%s %s' % (s['forename'], s['surname']))
                pib = take(by_name.get((sn, fn), []))
            if pib:
                slug = '%03d' % pib
                save(pib, slug); s['photo'] = slug; matched += 1
        if guessed:
            print('WARN two students share a name, so their photos are a guess: %s'
                  % ', '.join(sorted(set(guessed))))
            print('WARN   add the Students Simple Report as well and it can tell them apart')
        if not pairs and pics and len(pics) == len(students):
            print('WARN badge names unreadable - photos matched by position only')
            order = sorted(students, key=lambda s: (s['surname'].lower(), s['forename'].lower()))
            for i, s in enumerate(order):
                save(i + 1, '%03d' % (i + 1)); s['photo'] = '%03d' % (i + 1); matched += 1
        print('INFO students matched to a photo: %d' % matched)
        if matched < len(students):
            print('WARN students with no photo: %d' % (len(students) - matched))
    else:
        print('INFO no badge file given - the cards will show a cake instead of a face')

    # ------------------------------------- keep preferred names already typed in
    old = os.path.join(out_dir, 'students.csv')
    carried = 0
    if os.path.exists(old):
        prev = {}
        with open(old, newline='') as f:
            for r in csv.DictReader(f):
                if r.get('preferred_name'):
                    prev[(norm(r['surname']), norm(r['forename']),
                          r.get('day'), r.get('month'))] = r['preferred_name']
        for s in students:
            if s['preferred_name']: continue
            k = (norm(s['surname']), norm(s['forename']), str(s['day']), str(s['month']))
            if k in prev: s['preferred_name'] = prev[k]; carried += 1
    if carried: print('INFO preferred names carried over from the previous import: %d' % carried)
    got = sum(1 for s in students if s['preferred_name'])
    if got: print('INFO preferred names in use: %d' % got)

    students.sort(key=lambda s: (s['month'], s['day'], s['surname']))
    os.makedirs(out_dir, exist_ok=True)
    cols_out = ['forename', 'surname', 'preferred_name', 'day', 'month', 'birth_year',
                'year_group', 'form', 'house', 'gender', 'is_tutee',
                'enrolled_year', 'school_status', 'photo']
    with open(os.path.join(out_dir, 'students.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols_out, lineterminator='\n')
        w.writeheader(); w.writerows(students)
    tut = sum(1 for s in students if s['is_tutee'])
    print('INFO tutor group members flagged: %d' % tut)
    if initials and not tut:
        print('WARN no student has tutor initials "%s" - check the initials you entered' % initials)
    print('OK %d students written' % len(students))
    return 0


if __name__ == '__main__':
    sys.exit(main())
