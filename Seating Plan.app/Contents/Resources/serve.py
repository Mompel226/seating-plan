#!/usr/bin/env python3
"""Local helper for the seating planner.

    serve.py <base_dir> --daemon <urlfile> <errfile>

Binds to 127.0.0.1 on a random port, requires a random key on every /api call,
detaches from the launcher and exits once the page stops sending heartbeats.
Nothing is ever sent off this machine.
"""
import sys, os, re, io, json, csv, zipfile, secrets, socket, subprocess, threading, time, shutil, tempfile
import atexit, signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else '.')
RES = os.path.join(BASE, 'Seating Plan.app', 'Contents', 'Resources')
IMPORT = os.path.join(RES, 'import.py')
PAGE = os.path.join(RES, 'plan.html')
CLASSES = os.path.join(BASE, 'Classes')
ROOMS = os.path.join(BASE, 'Rooms.json')
MAX_CLASS_ZIP = 400 * 1024 * 1024
TOKEN = secrets.token_urlsafe(18)
IDLE_SECONDS = 900        # a quarter of an hour of silence before the helper gives up
def _sweep_old_uploads():
    """A previous run that was force-quit or lost to a power cut leaves the spreadsheets
    it was handed sitting in the temp folder. They hold every student's name and date of
    birth, so clear out anything older than half an hour before starting."""
    import glob as _g
    cutoff = time.time() - 1800
    for d in _g.glob(os.path.join(tempfile.gettempdir(), 'seating-import-*')):
        try:
            if os.path.isdir(d) and os.path.getmtime(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


_sweep_old_uploads()
UPLOADS = tempfile.mkdtemp(prefix='seating-import-')


def _clean_uploads(*_a):
    shutil.rmtree(UPLOADS, ignore_errors=True)


atexit.register(_clean_uploads)
for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    try:
        signal.signal(_sig, lambda s, f: (_clean_uploads(), os._exit(0)))
    except (ValueError, OSError):
        pass
last_seen = time.time()

MIME = {'.html': 'text/html; charset=utf-8', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.css': 'text/css', '.js': 'text/javascript'}


# ------------------------------------------------------------------ classes
def safe(name):
    return re.sub(r'[/:\\\\]', '-', str(name or '').strip())[:80]


def class_dir(name):
    d = os.path.abspath(os.path.join(CLASSES, safe(name)))
    return d if d.startswith(os.path.abspath(CLASSES) + os.sep) else None


def classes():
    if not os.path.isdir(CLASSES): return []
    out = []
    for n in sorted(os.listdir(CLASSES)):
        d = os.path.join(CLASSES, n)
        f = os.path.join(d, 'students.csv')
        if not os.path.isfile(f): continue
        try:
            with open(f, newline='') as fh: rows = list(csv.DictReader(fh))
        except OSError:
            rows = []
        plan = read_plan(n)
        p = current_plan(plan)
        plist = plan.get('plans')
        if not (isinstance(plist, list) and plist): plist = [p]
        nplans = len(plist)
        # The furniture of every plan, so the Plans dialog can list every layout the
        # teacher has ever drawn without a second round trip. Chairs and tables only -
        # a class never sees another class's seating.
        out.append({'name': n, 'students': len(rows),
                    'seats': count_seats(p.get('layout')),
                    'seated': len([v for v in (p.get('seats') or {}).values() if v]),
                    'plans': nplans,
                    'layouts': [{'plan': q.get('name') or 'Plan',
                                 'seats': count_seats(q.get('layout')),
                                 'layout': q.get('layout')}
                                for q in plist if q.get('layout')]})
    return out


def active():
    p = os.path.join(BASE, 'active_class')
    if os.path.isfile(p):
        n = open(p).read().strip()
        if n and os.path.isdir(os.path.join(CLASSES, n)): return n
    c = classes()
    return c[0]['name'] if c else ''


def set_active(name):
    with open(os.path.join(BASE, 'active_class'), 'w') as f: f.write(name + '\n')


def rename_class(old, new):
    """A class is a folder, so renaming one renames the folder - and then mends whatever
    was pointing at the old name. The only thing that does is the list of classes a
    printout covers, which every class keeps with its own print settings. Leave those
    alone and a renamed class quietly drops out of next term's printout without ever
    saying so, which is the sort of thing you only notice at the photocopier."""
    new = re.sub(r'[\x00-\x1f\x7f]', ' ', str(new or ''))
    src, dst = class_dir(old), class_dir(new)
    if not src or not os.path.isdir(src): return 'no such class', None
    if not dst: return 'give the class a name', None
    a, b = os.path.basename(src), os.path.basename(dst)
    if not b or b.startswith('.'):
        return 'a class name cannot be empty or start with a full stop', None
    if a == b: return None, a
    if a.lower() == b.lower():
        # only the capitals changed, and the disk may not tell the two names apart
        tmp = os.path.join(CLASSES, a + '.renaming')
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        if os.path.exists(dst): return 'there is already a class called "%s"' % b, None
        os.rename(src, dst)

    p = os.path.join(BASE, 'active_class')
    if os.path.isfile(p) and open(p).read().strip() == a:
        set_active(b)
    for c in os.listdir(CLASSES):
        f = os.path.join(CLASSES, c, 'plan.json')
        if not os.path.isfile(f): continue
        try:
            with open(f) as fh: d = json.load(fh)
        except Exception:
            continue
        pr = d.get('print')
        if not isinstance(pr, dict): continue
        lst = pr.get('classes')
        if not isinstance(lst, list) or a not in lst: continue
        pr['classes'] = [b if x == a else x for x in lst]
        write_plan(c, d)
    return None, b


def _is_new(r):
    """First year in the school: the year group they enrolled in is the one they are in.
    Both arrive as free text ('Year 10', '10'), so compare the numbers. Classes imported
    before the enrolment year was read have no such column, and simply never say new."""
    got = re.search(r'\d+', (r.get('enrolled_year') or ''))
    now = re.search(r'\d+', (r.get('year_group') or ''))
    return bool(got and now and got.group(0) == now.group(0))


def students(name):
    d = class_dir(name)
    if not d: return []
    f = os.path.join(d, 'students.csv')
    if not os.path.isfile(f): return []
    out = []
    with open(f, newline='') as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            first = (r.get('preferred_name') or '').strip() or (r.get('forename') or '').strip()
            out.append({
                'id': '%s|%s|%s|%s' % (r.get('surname', ''), r.get('forename', ''),
                                       r.get('day', ''), r.get('month', '')),
                'name': (first + ' ' + (r.get('surname') or '')).strip(),
                'first': first,
                'legal': ((r.get('forename') or '') + ' ' + (r.get('surname') or '')).strip()
                         if (r.get('preferred_name') or '').strip() else '',
                'surname': (r.get('surname') or '').strip(),
                'forename': (r.get('forename') or '').strip(),   # the legal one, for printing
                'year': (r.get('year_group') or '').strip(),
                'form': (r.get('form') or '').strip(),
                'house': (r.get('house') or '').strip(),
                'gender': (r.get('gender') or '').strip()[:1].upper(),
                'dob': '%s/%s/%s' % (r.get('day', ''), r.get('month', ''), r.get('birth_year', '')),
                'birth_year': (r.get('birth_year') or '').strip(),
                'tutee': bool((r.get('is_tutee') or '').strip()),
                # Enrolment Year is the year group they joined in. Equal to their year
                # group means this is their first year here - the "New" badge.
                'enrolled': (r.get('enrolled_year') or '').strip(),
                'status': (r.get('school_status') or '').strip(),
                'isNew': _is_new(r),
                'photo': (r.get('photo') or '').strip(),
            })
    return out


def read_plan(name):
    d = class_dir(name)
    if not d: return {}
    f = os.path.join(d, 'plan.json')
    if os.path.isfile(f):
        try: return json.load(open(f))
        except Exception: return {}
    return {}


def write_plan(name, plan):
    d = class_dir(name)
    if not d: return False
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, 'plan.json.tmp')
    with open(tmp, 'w') as f: json.dump(plan, f, indent=1)
    os.replace(tmp, os.path.join(d, 'plan.json'))
    return True


def count_seats(layout):
    if not layout: return 0
    n = 0
    for it in (layout.get('items') or []):
        if it.get('type') != 'table': continue
        for e in ('top', 'bottom', 'left', 'right'):
            try: n += int((it.get('seats') or {}).get(e) or 0)
            except (TypeError, ValueError): pass
    return n


# ------------------------------------------------------------ the room library
def rooms():
    """Saved room layouts, shared by every class. Furniture only - no students."""
    if not os.path.isfile(ROOMS): return []
    try:
        r = json.load(open(ROOMS))
        return r if isinstance(r, list) else []
    except Exception:
        return []


def write_rooms(lst):
    tmp = ROOMS + '.tmp'
    with open(tmp, 'w') as f: json.dump(lst, f, indent=1)
    os.replace(tmp, ROOMS)


# ------------------------------------------------- handing a class to a colleague
# ------------------------------------------------------- the roll, one student at a time
CSV_COLUMNS = ['forename', 'surname', 'preferred_name', 'day', 'month', 'birth_year',
               'year_group', 'form', 'house', 'gender', 'is_tutee', 'enrolled_year',
               'school_status', 'photo']


def roster_path(name):
    d = class_dir(name)
    return os.path.join(d, 'students.csv') if d else None


def row_id(r):
    """The same identity the rest of the app uses, so a row can be matched to a card."""
    return '%s|%s|%s|%s' % (r.get('surname', ''), r.get('forename', ''),
                            r.get('day', ''), r.get('month', ''))


def read_roster(name):
    f = roster_path(name)
    if not f or not os.path.isfile(f): return None, []
    with open(f, newline='') as fh:
        rd = csv.DictReader(fh)
        return (rd.fieldnames or list(CSV_COLUMNS)), [dict(r) for r in rd]


def write_roster(name, cols, rows):
    """Written whole, through a temporary file, keeping the version before it.

    A class list is a list of real children and this is the only place the app edits one
    in place, so the previous copy is always one file away - a removal made by mistake at
    half past eight in the morning is recoverable."""
    f = roster_path(name)
    if not f: return False
    if os.path.isfile(f):
        try: shutil.copyfile(f, f + '.previous')
        except OSError: pass
    tmp = f + '.tmp'
    with open(tmp, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore',
                           lineterminator='\n')   # as import.py writes it
        w.writeheader()
        for r in rows: w.writerow(r)
    os.replace(tmp, f)
    return True


def drop_photo(name, photo, rows):
    """A photograph belongs to the student who left, unless somebody else shares it."""
    if not photo: return
    if any((r.get('photo') or '').strip() == photo for r in rows): return
    d = class_dir(name)
    if not d: return
    for ext in ('.jpg', '.jpeg', '.png'):
        p = os.path.join(d, 'photos', photo + ext)
        if os.path.isfile(p):
            try: os.remove(p)
            except OSError: pass


def next_photo_slot(name):
    """The next free NNN. Photographs are numbered, not named after anybody - a folder of
    files called after children is exactly what this app is careful not to leave lying
    about, and the class list is the only thing that says which is whose."""
    d = class_dir(name)
    pd = os.path.join(d, 'photos')
    os.makedirs(pd, exist_ok=True)
    used = set()
    for f in os.listdir(pd):
        stem = os.path.splitext(f)[0]
        if stem.isdigit(): used.add(int(stem))
    i = 1
    while i in used: i += 1
    return '%03d' % i


def save_photo(name, blob, suffix):
    """Written the size the rest of them are, so one card cannot carry a ten-megapixel
    portrait while the others carry thumbnails. sips ships with macOS; without it the
    picture is kept as it came."""
    d = class_dir(name)
    slot = next_photo_slot(name)
    dst = os.path.join(d, 'photos', slot + '.jpg')
    tmp = os.path.join(UPLOADS, 'photo_in' + (suffix or '.jpg'))
    with open(tmp, 'wb') as f: f.write(blob)
    done = False
    if shutil.which('sips'):
        try:
            r = subprocess.run(['sips', '-s', 'format', 'jpeg', '-Z', '480', tmp, '--out', dst],
                               capture_output=True, text=True, timeout=60)
            done = r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0
        except Exception:
            done = False
    if not done:
        shutil.copyfile(tmp, dst)
    try: os.remove(tmp)
    except OSError: pass
    return slot


def display_name(r):
    """What to call this student in a sentence addressed to the teacher."""
    first = (r.get('preferred_name') or '').strip() or (r.get('forename') or '').strip()
    return (first + ' ' + (r.get('surname') or '')).strip()


def match_key(surname, forename, day, month):
    """Who this is, for the purpose of telling an arrival from somebody already here.

    The seat map remembers a student as "Surname|Forename|day|month", so that is the
    identity that has to be matched on: get it right and everybody already in the room
    keeps their chair. Case and punctuation are ignored, because iSAMS is not always
    consistent about either from one export to the next."""
    return (re.sub(r'[^a-z]', '', str(surname or '').lower()),
            re.sub(r'[^a-z]', '', str(forename or '').lower()),
            re.sub(r'\D', '', str(day or '')),
            re.sub(r'\D', '', str(month or '')))


def row_key(r):
    return match_key(r.get('surname'), r.get('forename'), r.get('day'), r.get('month'))


def take_photo_from(name, staged_dir, slug):
    """Copy one face out of a staged import into this class, in the next free slot.

    Renumbered on the way in rather than keeping the number it had: the staged import
    numbered its photographs 1..23 for a class whose own photographs are already
    numbered 1..21, and a straight copy would overwrite twenty-one faces."""
    if not slug: return ''
    src = ''
    for ext in ('.jpg', '.jpeg', '.png'):
        p = os.path.join(staged_dir, 'photos', slug + ext)
        if os.path.isfile(p): src = p; break
    if not src: return ''
    d = class_dir(name)
    if not d: return ''
    slot = next_photo_slot(name)
    try:
        shutil.copyfile(src, os.path.join(d, 'photos', slot + '.jpg'))
    except OSError:
        return ''
    return slot


# Never rewritten by an update. These four are the identity the seat map is keyed on, and
# altering one would quietly empty that student's chair.
IDENTITY = ('surname', 'forename', 'day', 'month')


def merge_class(name, staged_dir):
    """Fold a fresh iSAMS export into a class that already exists.

    The photograph is the whole reason this exists. A student who joins in week three
    can be typed in by hand in half a minute, but a face cannot be typed, and going to
    fetch one out of iSAMS a child at a time is the part that hurts. Importing the class
    again from scratch would fix the photograph and cost everything else: it is the same
    twenty-one students, and every chair, every label, every note and every keep-apart
    pair would have to be set out a second time. So the whole class is exported again and
    only the difference is taken.

    Nobody already on the roll is moved, renumbered or re-photographed, and nobody is
    ever removed. A name that has vanished from the export is reported and left exactly
    where it is: a student missing from a spreadsheet is not the same thing as a student
    who has left the class, and only the teacher can tell which of the two it is."""
    cols, rows = read_roster(name)
    if cols is None:
        return None, 'that class could not be found'
    # A class imported before a column existed still has to be able to receive one.
    cols = list(cols) + [c for c in CSV_COLUMNS if c not in cols]
    staged = os.path.join(staged_dir, 'students.csv')
    if not os.path.isfile(staged):
        return None, 'those spreadsheets produced no class list'
    with open(staged, newline='') as fh:
        incoming = [dict(r) for r in csv.DictReader(fh)]
    if not incoming:
        return None, 'those spreadsheets held no students'

    # Held as queues, so that two students who share a name and a birthday - which does
    # happen - are matched one to one rather than both onto the first row.
    waiting = {}
    for r in rows: waiting.setdefault(row_key(r), []).append(r)

    added, gained, filled = [], [], 0
    for inc in incoming:
        q = waiting.get(row_key(inc)) or []
        here = q.pop(0) if q else None
        if here is None:
            row = {c: (inc.get(c) or '') for c in cols}
            row['photo'] = take_photo_from(name, staged_dir, (inc.get('photo') or '').strip())
            rows.append(row)
            added.append(display_name(row))
            continue
        # Already here. Fill in what was blank and nothing else: a form or a house that
        # has changed is the teacher's to change, and the four identity fields are not
        # anybody's to change without emptying a chair.
        for c in cols:
            if c in IDENTITY or c == 'photo': continue
            if not (here.get(c) or '').strip() and (inc.get(c) or '').strip():
                here[c] = inc[c]; filled += 1
        if not (here.get('photo') or '').strip():
            slot = take_photo_from(name, staged_dir, (inc.get('photo') or '').strip())
            if slot:
                here['photo'] = slot; gained.append(display_name(here))

    seen = {row_key(r) for r in incoming}
    missing = [display_name(r) for r in rows if row_key(r) not in seen]

    write_roster(name, cols, rows)
    return {'added': added, 'photos': gained, 'filled': filled,
            'missing': missing, 'total': len(rows)}, ''


def export_zip(name):
    """A .seatingclass file: everything about one class, and nothing about any other."""
    d = class_dir(name)
    if not d or not os.path.isdir(d): return None
    plan = read_plan(name)
    rows = students(name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps({
            'app': 'seating-plan', 'version': 1, 'name': name,
            'students': len(rows),
            'photos': len([r for r in rows if r.get('photo')]),
            'plans': len(plan.get('plans') or []) or 1,
            'has_labels': bool(plan.get('marks')),
            'has_notes': bool(plan.get('notes')),
            'has_keep_apart': bool(plan.get('pairs')),
        }, indent=1))
        for f in ('students.csv', 'plan.json'):
            p = os.path.join(d, f)
            if os.path.isfile(p): z.write(p, f)
        pd = os.path.join(d, 'photos')
        if os.path.isdir(pd):
            for f in sorted(os.listdir(pd)):
                if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                    z.write(os.path.join(pd, f), 'photos/' + f)
    return buf.getvalue()


PHOTO_OK = re.compile(r'^photos/[A-Za-z0-9._-]{1,60}\.(jpg|jpeg|png)$')


def import_zip(blob, want_name=''):
    """Unpack a .seatingclass. Only the four things we wrote are ever taken out of it."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except Exception:
        return None, 'that file is not a seating plan class'
    names = z.namelist()
    if 'manifest.json' not in names or 'students.csv' not in names:
        return None, 'that file is not a seating plan class'
    try:
        man = json.loads(z.read('manifest.json'))
    except Exception:
        return None, 'that file is damaged'
    if man.get('app') != 'seating-plan':
        return None, 'that file came from a different app'
    total = sum(i.file_size for i in z.infolist())
    if total > MAX_CLASS_ZIP:
        return None, 'that file is too big to be a class'

    name = safe(want_name or man.get('name') or 'Imported class')
    taken = {c['name'] for c in classes()}
    if name in taken:
        for i in range(2, 60):
            if '%s (%d)' % (name, i) not in taken: name = '%s (%d)' % (name, i); break
    out = class_dir(name)
    if not out: return None, 'that name cannot be used'
    os.makedirs(os.path.join(out, 'photos'), exist_ok=True)
    for n in names:
        if n in ('students.csv', 'plan.json'):
            with open(os.path.join(out, n), 'wb') as f: f.write(z.read(n))
        elif PHOTO_OK.match(n):
            with open(os.path.join(out, 'photos', os.path.basename(n)), 'wb') as f:
                f.write(z.read(n))
        # anything else in the archive is ignored on purpose
    return name, man


def current_plan(plan):
    """The plan the teacher has open. Older files hold a single plan at the top level."""
    plans = plan.get('plans')
    if isinstance(plans, list) and plans:
        for p in plans:
            if p.get('id') == plan.get('current'): return p
        return plans[0]
    return plan


def drop_uploads(paths):
    """The spreadsheets have done their job the moment the import succeeds. They are the
    only copy of your students outside the app folder, so they do not hang about."""
    for p in paths:
        try:
            if p and os.path.isfile(p) and os.path.dirname(os.path.abspath(p)) == UPLOADS:
                os.remove(p)
        except OSError:
            pass


def python_exe():
    for c in (sys.executable, '/usr/bin/python3', '/usr/local/bin/python3',
              '/opt/homebrew/bin/python3'):
        if c and os.path.exists(c): return c
    return 'python3'


def page_html():
    html = open(PAGE, encoding='utf-8').read()
    return html.replace('/*__LIVE__*/',
                        'window.TOKEN = %s;' % json.dumps(TOKEN)).encode('utf-8')


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a): pass

    def send(self, code, body=b'', ctype='application/json'):
        if isinstance(body, str): body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try: self.wfile.write(body)
        except BrokenPipeError: pass

    def js(self, obj, code=200): self.send(code, json.dumps(obj))

    def authed(self, q):
        return q.get('t', [''])[0] == TOKEN or self.headers.get('X-Token', '') == TOKEN

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        parts, left = [], n
        while left > 0:
            chunk = self.rfile.read(min(left, 1 << 20))
            if not chunk: break
            parts.append(chunk); left -= len(chunk)
        return b''.join(parts)

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        global last_seen
        u = urlparse(self.path); q = parse_qs(u.query); path = unquote(u.path)
        last_seen = time.time()

        if path in ('/', '/index.html'):
            return self.send(200, page_html(), 'text/html; charset=utf-8')

        if path == '/api/state':
            if not self.authed(q): return self.js({'error': 'bad key'}, 403)
            a = active()
            return self.js({'classes': classes(), 'active': a, 'rooms': rooms(),
                            'students': students(a) if a else [],
                            'plan': read_plan(a) if a else {}})

        if path == '/api/class':
            # One class, read only, without making it the open one - what printing several
            # classes in a single job needs.
            if not self.authed(q): return self.js({'error': 'bad key'}, 403)
            name = q.get('name', [''])[0]
            d = class_dir(name)
            if not d or not os.path.isdir(d): return self.js({'error': 'no such class'}, 400)
            return self.js({'name': name, 'students': students(name), 'plan': read_plan(name)})

        if path == '/api/export':
            if not self.authed(q): return self.js({'error': 'bad key'}, 403)
            name = q.get('class', [''])[0] or active()
            blob = export_zip(name)
            if blob is None: return self.js({'error': 'no such class'}, 400)
            fn = re.sub(r'[^A-Za-z0-9 ._-]', '_', name) + '.seatingclass'
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(len(blob)))
            self.send_header('Content-Disposition', 'attachment; filename="%s"' % fn)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try: self.wfile.write(blob)
            except BrokenPipeError: pass
            return

        target = os.path.abspath(os.path.join(BASE, path.lstrip('/')))
        if not target.startswith(BASE + os.sep) or not os.path.isfile(target):
            return self.send(404, b'not found', 'text/plain')
        with open(target, 'rb') as f: data = f.read()
        return self.send(200, data, MIME.get(os.path.splitext(target)[1].lower(),
                                             'application/octet-stream'))

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        global last_seen
        u = urlparse(self.path); q = parse_qs(u.query)
        last_seen = time.time()
        if not self.authed(q): return self.js({'error': 'bad key'}, 403)
        path = u.path

        if path == '/api/heartbeat':
            return self.js({'ok': True})

        if path == '/api/switch':
            name = (json.loads(self.body() or b'{}') or {}).get('name', '')
            if not any(c['name'] == name for c in classes()):
                return self.js({'error': 'no such class'}, 400)
            set_active(name)
            return self.js({'ok': True, 'active': name})

        if path == '/api/rename':
            d = json.loads(self.body() or b'{}') or {}
            err, to = rename_class(d.get('name', ''), d.get('to', ''))
            if err: return self.js({'error': err}, 400)
            return self.js({'ok': True, 'name': to, 'active': active(),
                            'classes': classes()})

        if path == '/api/delete':
            name = (json.loads(self.body() or b'{}') or {}).get('name', '')
            d = class_dir(name)
            if not d or not os.path.isdir(d): return self.js({'error': 'no such class'}, 400)
            shutil.rmtree(d, ignore_errors=True)
            left = classes()
            set_active(left[0]['name'] if left else '')
            return self.js({'ok': True, 'active': active(), 'classes': left})

        if path == '/api/plan':
            d = json.loads(self.body() or b'{}') or {}
            name = d.get('class') or active()
            if not class_dir(name): return self.js({'error': 'no such class'}, 400)
            plan = read_plan(name)
            for k in ('plans', 'current', 'layout', 'seats',
                      'tags', 'marks', 'pairs', 'pins', 'notes', 'show', 'note',
                      'print'):                    # what the printout should carry
                if k in d: plan[k] = d[k]
            if 'plans' in d:                       # the old single-plan keys are gone for good
                for k in ('layout', 'seats', 'note'): plan.pop(k, None)
            write_plan(name, plan)
            return self.js({'ok': True, 'seats': count_seats(current_plan(plan).get('layout'))})

        if path == '/api/rooms':
            d = json.loads(self.body() or b'{}') or {}
            act, name = d.get('action', 'save'), safe(d.get('name', ''))
            lst = rooms()
            if act == 'delete':
                write_rooms([r for r in lst if r.get('name') != name])
            elif act == 'save':
                if not name: return self.js({'error': 'give the room a name'}, 400)
                lay = d.get('layout') or {}
                if not (lay.get('items') or []): return self.js({'error': 'that room is empty'}, 400)
                lst = [r for r in lst if r.get('name') != name]
                lst.append({'name': name, 'layout': lay})
                lst.sort(key=lambda r: r.get('name', '').lower())
                write_rooms(lst)
            elif act == 'rename':
                to = safe(d.get('to', ''))
                if not to: return self.js({'error': 'give the room a name'}, 400)
                for r in lst:
                    if r.get('name') == name: r['name'] = to
                lst.sort(key=lambda r: r.get('name', '').lower())
                write_rooms(lst)
            else:
                return self.js({'error': 'unknown action'}, 400)
            return self.js({'ok': True, 'rooms': rooms()})

        if path == '/api/importclass':
            blob = self.body()
            name, man = import_zip(blob, q.get('name', [''])[0])
            if not name: return self.js({'error': man}, 400)
            set_active(name)
            return self.js({'ok': True, 'active': name, 'manifest': man,
                            'classes': classes(), 'students': students(name)})

        if path == '/api/copylayout':
            d = json.loads(self.body() or b'{}') or {}
            src, dst = d.get('from', ''), d.get('to') or active()
            if not class_dir(src) or not class_dir(dst):
                return self.js({'error': 'no such class'}, 400)
            sp = current_plan(read_plan(src))
            plan = read_plan(dst)
            p = current_plan(plan)
            p['layout'] = sp.get('layout')
            p['seats'] = {}
            if p is plan:                       # an old single-plan file: keep it that shape
                plan['layout'] = p['layout']; plan['seats'] = {}
            # the label vocabulary comes too, but only while nobody in the destination
            # has been given a label yet - otherwise their labels would point at nothing
            if read_plan(src).get('tags') and not (plan.get('marks') or {}):
                plan['tags'] = read_plan(src)['tags']
            write_plan(dst, plan)
            return self.js({'ok': True})

        if path == '/api/upload':
            kind = q.get('kind', ['report'])[0]
            if kind not in ('report', 'badges', 'extra'):
                return self.js({'error': 'bad kind'}, 400)
            body = self.body()
            if not body:
                return self.js({'error': 'that file arrived empty - drag it out of a Finder '
                                         'window rather than the Downloads stack, or click '
                                         'the box and choose it'}, 400)
            dest = upload_dest(kind, q.get('name', [kind])[0])
            with open(dest, 'wb') as f: f.write(body)
            return self.js(count_upload(kind, dest))

        # The same file, taken by its path. When a drag comes from the Downloads stack or
        # a browser's download bar, macOS hands the page the location of the file rather
        # than the file itself, and the page could do nothing with that - which is why
        # dropping a spreadsheet appeared not to work at all and you had to click instead.
        # Reading it here is no wider a door than the file chooser already is: the helper
        # answers only this Mac, only with this run's key, and only reads what was dropped.
        if path == '/api/uploadpath':
            d = json.loads(self.body() or b'{}') or {}
            kind = d.get('kind', 'report')
            if kind not in ('report', 'badges', 'extra'):
                return self.js({'error': 'bad kind'}, 400)
            src_path = file_url_to_path(d.get('path', ''))
            if not src_path or not os.path.isfile(src_path):
                return self.js({'error': 'that file could not be found where it was dropped '
                                         'from - click the box and choose it instead'}, 400)
            try:
                if os.path.getsize(src_path) > MAX_CLASS_ZIP:
                    return self.js({'error': 'that file is too big to be a class list'}, 400)
                dest = upload_dest(kind, os.path.basename(src_path))
                shutil.copyfile(src_path, dest)
            except OSError as e:
                return self.js({'error': 'that file could not be read: %s' % e}, 400)
            return self.js(count_upload(kind, dest))

        if path == '/api/roster':
            d = json.loads(self.body() or b'{}') or {}
            cls = d.get('class', '')
            cols, rows = read_roster(cls)
            if cols is None:
                return self.js({'error': 'that class could not be found'}, 404)
            act = d.get('action', '')

            if act == 'remove':
                who = d.get('id', '')
                keep = [r for r in rows if row_id(r) != who]
                if len(keep) == len(rows):
                    return self.js({'error': 'that student is not on this class list'}, 404)
                gone = [r for r in rows if row_id(r) == who][0]
                write_roster(cls, cols, keep)
                drop_photo(cls, (gone.get('photo') or '').strip(), keep)
                return self.js({'ok': True, 'students': students(cls),
                                'classes': classes()})

            if act == 'add':
                s = d.get('student') or {}
                sur = (s.get('surname') or '').strip()
                fore = (s.get('forename') or '').strip()
                if not sur or not fore:
                    return self.js({'error': 'a surname and a first name are needed'}, 400)
                row = {c: '' for c in cols}
                row.update({
                    'surname': sur, 'forename': fore,
                    'preferred_name': (s.get('preferred_name') or '').strip(),
                    'day': re.sub(r'\D', '', (s.get('day') or ''))[:2],
                    'month': re.sub(r'\D', '', (s.get('month') or ''))[:2],
                    'birth_year': re.sub(r'\D', '', (s.get('birth_year') or ''))[:4],
                    'year_group': (s.get('year_group') or '').strip(),
                    'form': (s.get('form') or '').strip(),
                    'gender': (s.get('gender') or '').strip()[:1].upper(),
                    'photo': '',
                })
                if any(row_id(r) == row_id(row) for r in rows):
                    return self.js({'error': 'somebody with that name and birthday is already '
                                             'on this class list'}, 400)
                rows.append(row)
                write_roster(cls, cols, rows)
                return self.js({'ok': True, 'students': students(cls),
                                'classes': classes()})

            return self.js({'error': 'unknown roster action'}, 400)

        if path == '/api/photo':
            cls = q.get('class', [''])[0]
            who = q.get('id', [''])[0]
            cols, rows = read_roster(cls)
            if cols is None:
                return self.js({'error': 'that class could not be found'}, 404)
            row = next((r for r in rows if row_id(r) == who), None)
            if row is None:
                return self.js({'error': 'that student is not on this class list'}, 404)
            old = (row.get('photo') or '').strip()

            if q.get('action', [''])[0] == 'clear':
                row['photo'] = ''
                write_roster(cls, cols, rows)
                drop_photo(cls, old, rows)
                return self.js({'ok': True, 'students': students(cls)})

            blob = self.body()
            if not blob:
                return self.js({'error': 'that picture arrived empty'}, 400)
            if len(blob) > 40 * 1024 * 1024:
                return self.js({'error': 'that picture is too big'}, 400)
            # only the kinds a card can actually show
            head = blob[:12]
            if not (head[:3] == b'\xff\xd8\xff' or head[:8] == b'\x89PNG\r\n\x1a\n'
                    or head[:4] == b'RIFF' or head[4:12] == b'ftypheic'):
                return self.js({'error': 'that file is not a photograph - a JPEG, a PNG or a '
                                         'HEIC from an iPhone will do'}, 400)
            name_hint = q.get('name', [''])[0]
            suffix = os.path.splitext(name_hint)[1].lower()
            if not re.fullmatch(r'\.[A-Za-z0-9]{1,5}', suffix or ''): suffix = '.jpg'
            try:
                slot = save_photo(cls, blob, suffix)
            except Exception as e:
                return self.js({'error': 'that picture could not be read: %s' % e}, 400)
            row['photo'] = slot
            write_roster(cls, cols, rows)
            if old and old != slot: drop_photo(cls, old, rows)
            return self.js({'ok': True, 'photo': slot, 'students': students(cls)})

        if path == '/api/import':
            d = json.loads(self.body() or b'{}') or {}
            name = safe(d.get('name', ''))
            if not name: return self.js({'error': 'give the class a name'}, 400)
            extras = [e for e in (d.get('extras') or []) if e and os.path.isfile(e)]
            report = d.get('report', '')
            if not report or not os.path.isfile(report):
                # any spreadsheet with names and dates of birth will do as the roster,
                # so fall back to whatever else was sent rather than refusing
                report = extras.pop(0) if extras else ''
            if not report:
                return self.js({'error': 'no student list was received'}, 400)
            out = class_dir(name)
            os.makedirs(out, exist_ok=True)
            try:
                r = subprocess.run([python_exe(), IMPORT, report,
                                    d.get('badges') or '-', out, ''] + extras,
                                   capture_output=True, text=True, timeout=900)
            except Exception as e:
                return self.js({'error': str(e)[:300]}, 500)
            log = (r.stdout or '') + (r.stderr or '')
            if 'OK ' not in log:
                return self.js({'error': 'import failed', 'log': log[-1500:]}, 500)
            drop_uploads([report, d.get('badges')] + extras)
            set_active(name)
            return self.js({'ok': True, 'active': name, 'log': log,
                            'classes': classes(), 'students': students(name)})

        # Whoever is new, and nobody else. The same three spreadsheets as a fresh import,
        # read by the same importer into a scratch folder, and only the difference taken
        # across - so that a class gains the two students who joined in September without
        # losing the seating, the labels and the notes built up around the other twenty-one.
        if path == '/api/updateclass':
            d = json.loads(self.body() or b'{}') or {}
            name = d.get('class', '')
            cdir = class_dir(name)
            if not cdir or not os.path.isdir(cdir):
                return self.js({'error': 'no such class'}, 400)
            extras = [e for e in (d.get('extras') or []) if e and os.path.isfile(e)]
            report = d.get('report', '')
            if not report or not os.path.isfile(report):
                report = extras.pop(0) if extras else ''
            if not report:
                return self.js({'error': 'no student list was received'}, 400)
            staged = tempfile.mkdtemp(prefix='update-', dir=UPLOADS)
            try:
                try:
                    r = subprocess.run([python_exe(), IMPORT, report,
                                        d.get('badges') or '-', staged, ''] + extras,
                                       capture_output=True, text=True, timeout=900)
                except Exception as e:
                    return self.js({'error': str(e)[:300]}, 500)
                log = (r.stdout or '') + (r.stderr or '')
                if 'OK ' not in log:
                    return self.js({'error': 'those spreadsheets could not be read',
                                    'log': log[-1500:]}, 500)
                summary, err = merge_class(name, staged)
            finally:
                shutil.rmtree(staged, ignore_errors=True)
            if err:
                return self.js({'error': err}, 400)
            drop_uploads([report, d.get('badges')] + extras)
            return self.js({'ok': True, 'report': summary, 'log': log,
                            'classes': classes(), 'students': students(name)})

        return self.js({'error': 'unknown'}, 404)


def upload_dest(kind, raw):
    """Where a dropped or chosen spreadsheet is put while it is being read.

    Keep the extension whatever the rest of the name looks like: a download named
    "export (1).xlsx" must still arrive as .xlsx. Spaces and brackets are only flattened
    for tidiness - every path here is passed as an argv element, never through a shell,
    so they were never dangerous."""
    stem, ext = os.path.splitext(os.path.basename(raw))
    ext = ext if re.fullmatch(r'\.[A-Za-z0-9]{1,8}', ext or '') else ''
    fn = (re.sub(r'[^A-Za-z0-9._-]', '_', stem)[-70:] or kind) + ext
    return os.path.join(UPLOADS, '%s_%s' % (kind, fn))


def count_upload(kind, dest):
    """How many students the spreadsheet turned out to hold, for the checklist."""
    info = {'ok': True, 'path': dest, 'bytes': os.path.getsize(dest)}
    if kind in ('report', 'extra'):
        try:
            r = subprocess.run([python_exe(), IMPORT, '--count', dest],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                info['students'] = int(r.stdout.strip())
            else:
                info['warning'] = (r.stderr or r.stdout or '')[:300]
        except Exception as e:
            info['warning'] = str(e)[:200]
    return info


def file_url_to_path(raw):
    """The first usable local path out of what a drag left on the pasteboard - a
    file:// URL, or already a path. Anything else, and anything not on this Mac, is
    refused rather than guessed at."""
    s = (raw or '').strip().split('\n')[0].strip().strip('\r')
    if not s:
        return ''
    if s.startswith('file://'):
        parts = urlparse(s)
        if parts.netloc not in ('', 'localhost'):
            return ''
        s = unquote(parts.path)
    if not s.startswith('/'):
        return ''
    return os.path.realpath(s)


def idle_watch(httpd):
    """Stop once the page has gone, and not a moment before it has.

    The page checks in every so often; a long silence means the window is closed. Two
    things used to end this helper while the window was still on screen, and both left
    that window looking perfectly normal while nothing it did reached the disk:

      * a Mac that went to sleep, because the clock kept running while the page could
        not possibly check in - so the clock is only allowed to run while we are awake,
        and a step that took far longer than it was asked to is treated as a nap;
      * a heartbeat that was slowed down by macOS in a window sitting behind others,
        which two and a half minutes had no room for.
    """
    step = 15
    global last_seen
    while True:
        before = time.time()
        time.sleep(step)
        if time.time() - before > step * 4:      # the Mac was asleep, not the page
            last_seen = time.time()
            continue
        if time.time() - last_seen > IDLE_SECONDS:
            shutil.rmtree(UPLOADS, ignore_errors=True)
            httpd.shutdown(); return


def daemonize(errfile):
    if os.fork() > 0: os._exit(0)
    os.setsid()
    if os.fork() > 0: os._exit(0)
    sys.stdout.flush(); sys.stderr.flush()
    null = os.open(os.devnull, os.O_RDWR); os.dup2(null, 0)
    err = os.open(errfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(err, 1); os.dup2(err, 2)


def main():
    urlfile = None
    if '--daemon' in sys.argv:
        i = sys.argv.index('--daemon')
        urlfile = sys.argv[i + 1]
        daemonize(sys.argv[i + 2] if len(sys.argv) > i + 2 else os.devnull)
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    httpd = ThreadingHTTPServer(('127.0.0.1', port), H)
    threading.Thread(target=idle_watch, args=(httpd,), daemon=True).start()
    url = 'http://127.0.0.1:%d/?t=%s' % (port, TOKEN)
    if urlfile:
        tmp = urlfile + '.tmp'
        with open(tmp, 'w') as f: f.write(url + '\n')
        os.replace(tmp, urlfile)
    else:
        print('READY ' + url, flush=True)
    try:
        httpd.serve_forever()
    finally:
        shutil.rmtree(UPLOADS, ignore_errors=True)
        if urlfile:
            try: os.remove(urlfile)
            except OSError: pass


if __name__ == '__main__':
    main()
