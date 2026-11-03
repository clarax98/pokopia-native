#!/usr/bin/env python3
"""nxs2.py: build Pokemon Pokopia (Nintendo Switch 2) for your own Mac and iPhone, from your own dump.

Everything derived from the game is made here, on your machine, from the dump you give; nothing of the
game is in this repository (see LEGAL.md). The build goes through nxnative (the native host for Switch
games on Apple systems, with this project's Switch 2 changes), the game's shaders through ampere-lower
(Eden-NS2's Switch 2 shader lowering) and nxnative's Metal shader chain.

  nxs2.py setup                             fetch nxnative at the pinned commit, apply the Switch 2 patch, build ampere-lower
  nxs2.py doctor                            check the tools this Mac has (what setup and build need)
  nxs2.py build <dump> [options]            the whole build into <dump folder>/<title>_native (or --out)
       --mac-app         a double-clickable Mac app (the game files cloned inside it)
       --ipa             an unsigned IPA with the entitlements the game needs (sign it with your own team)
       --xcode --team-id ID   an Xcode project signed with your Apple developer team (then: nxs2.py sign)
       --dumps DIR...    shader dumps from an earlier run (DM_NVN_SHDUMP folders) to include from the start
       --no-ios          Mac libraries only (faster; --ipa / --xcode need the iOS ones)
  nxs2.py run <build dir>                   play in a window on this Mac; new shaders are recorded for refresh
  nxs2.py refresh <build dir> [DIR...]      add the shaders recorded while playing (Mac run, Mac app, iPhone) and rebuild the outputs
  nxs2.py sign <build dir> --team-id ID     archive and export the development IPA from the prepared Xcode project
  nxs2.py status <build dir>                what the build folder holds and what it still lacks

Needs: a Mac with Apple silicon, Xcode (27.x) with its command line tools (an iPhone build also needs the iOS
platform), Python 3.12 or newer with capstone (5.x), lz4 and fontTools, the .NET 10 runtime (the shader
translator), xcodegen for the iPhone build. `nxs2.py doctor` lists what is missing and how to get it.
"""
import argparse, concurrent.futures as cf, hashlib, json, os, pathlib, platform, re, shutil, subprocess, sys, time

TITLE = os.environ.get('NXS2_TITLE', '0400A3A01EE4C000')
VERSION = int(os.environ.get('NXS2_VERSION', '327680'))
NAME = os.environ.get('NXS2_NAME', 'Pokemon Pokopia')
UPSTREAM = 'https://github.com/seedds/nxnative'
UPSTREAM_COMMIT = 'c1df4dd'   # the nxnative commit host/nxnative-switch2.patch is made against
HERE = pathlib.Path(__file__).resolve().parent
PROFILE_SRC = HERE / 'profile' / TITLE
TOOLS = HERE / 'tools'
AMPERE = HERE / 'ampere-lower' / 'ampere-lower'
NX = pathlib.Path(os.environ.get('NXNATIVE') or HERE / 'nxnative')
PY = sys.executable
T0 = time.time()

# the shader tables a profile has before the package is made (as nxnative's NSP script writes them for a provisional profile)
EMPTY_TABLES = {
    'shader_map.tsv': '# kind: evidence  filled by the shader package (nxs2.py package)\npackage\tindex\tstage\tdm_ctl_sha16\tsource\n',
    'shader_facts.tsv': '# Answer table for translation queries: key <TAB> fact JSON. Filled by the shader package (nxs2.py package)\n',
    'shader_pairs.tsv': '# kind: evidence  vertex / pixel shader pairs within BNSH variations: vertex key <TAB> pixel key\n',
    'shader_map.tsv.conflicts.tsv': '# kind: evidence  control-section key conflict rulings: empty\ndm_ctl_sha16\tchosen_pc\tclass\tcandidates\trecords\n',
    'bf_want.tsv': '# generated\n',
    'pso_manifest.tsv': '# Prewarm seed list (shipped in the app, next to shader_map.tsv). Rows: S<TAB>shader key<TAB>stage / R<TAB>render pipeline key (pso_skey hex) / C<TAB>compute shader key.\n'
                        '# Empty until recorded on the Mac (DM_PSO_MANIFEST=off DM_PSO_RECORD).\n'}
# switches baked into the apps (the player's env.txt overrides them): the shaders the game makes while it runs are recorded for `nxs2.py refresh`
APP_ENV = '# nxs2.py: switches the build bakes in; Documents/env.txt (iPhone) or Application Support/nxnative/<title>/env.txt (Mac) override them\nDM_NVN_SHDUMP=shdump\n'
PHONE_ENV = APP_ENV + 'DM_OPMODE=0\nDM_FPS=30\n'   # handheld mode and 30 fps on the phone: what the Switch 2 does in handheld, and what keeps the phone cool


class Fail(Exception): pass


def hms(s):
    s = int(s); return f'{s // 60}:{s % 60:02d}' if s < 3600 else f'{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}'


def say(msg): print(f'[{hms(time.time() - T0)}] {msg}', flush=True)


def run(cmd, log, env=None, cwd=None, ok=(0,), heartbeat=30, show=None):
    """Run cmd with its output in log (a file), a heartbeat line on the console while it runs (and the lines show matches, or all of them with
    -v). A non-zero exit not in ok stops the build with the log's last lines."""
    cmd = [str(c) for c in cmd]
    log = pathlib.Path(log); log.parent.mkdir(parents=True, exist_ok=True)
    e = dict(os.environ); e.update(env or {})
    last, t_beat, t_start = '', time.time(), time.time()
    with open(log, 'w', encoding='utf-8', errors='replace') as f:
        f.write('$ ' + ' '.join(cmd) + '\n'); f.flush()
        p = subprocess.Popen(cmd, env=e, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
        for line in p.stdout:
            f.write(line); last = line.rstrip()
            if VERBOSE or (show and re.search(show, line)): print('    ' + last[:160], flush=True)
            if time.time() - t_beat > heartbeat:
                t_beat = time.time(); print(f'    ... {hms(time.time() - t_start)}  {last[:120]}', flush=True)
        rc = p.wait()
    if rc not in ok:
        tail = ''.join(log.read_text(encoding='utf-8', errors='replace').splitlines(keepends=True)[-25:])
        raise Fail(f'{pathlib.Path(cmd[0]).name} failed ({rc}); log: {log}\n{tail}')
    return rc


def which(name, *extra):
    p = shutil.which(name)
    if p: return p
    for x in extra:
        x = pathlib.Path(os.path.expanduser(x))
        if x.is_file() and os.access(x, os.X_OK): return str(x)
    return None


def dotnet_root():   # the .NET install that holds the runtime: on PATH, in ~/.dotnet, or Homebrew's
    d = which('dotnet', '~/.dotnet/dotnet', '/usr/local/share/dotnet/dotnet', '/opt/homebrew/opt/dotnet/libexec/dotnet')
    return pathlib.Path(d).resolve().parent if d else None


def fingerprint(*parts):
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, pathlib.Path):
            st = p.stat() if p.exists() else None
            h.update(f'{p}:{st.st_size if st else -1}:{int(st.st_mtime) if st else 0}\n'.encode())
        else: h.update(repr(p).encode() + b'\n')
    return h.hexdigest()[:16]


class Build:
    def __init__(self, out):
        self.out = pathlib.Path(out).resolve()
        self.logs = self.out / 'logs'; self.stamps = self.out / 'stamps'
        self.extract = self.out / 'extract'; self.exefs = self.extract / 'ExeFS'; self.romfs_img = self.extract / 'RomFS.img'
        self.patched = self.out / 'patched'; self.host = self.out / 'host' / 'dmhost'; self.font = self.out / 'font'
        self.profile_root = self.out / 'profile'; self.profile = self.profile_root / TITLE
        self.shader = self.out / 'shader'; self.corpus = self.shader / 'corpus'; self.ctl = self.shader / 'corpus-ctl'
        self.maxwell = self.shader / 'maxwell'; self.pkg = self.shader / 'pkg'; self.tables = self.shader / 'tables'
        self.cache = self.shader / 'cache'; self.ios = self.shader / 'ios'
        self.outputs = self.out / 'out'; self.rundir = self.out / 'run'; self.phone = self.out / 'phone'
        self.game = read_game_tsv(PROFILE_SRC / 'game.tsv')
        self.modules = self.game['modules'][0].split()
        self.build_ids = dict(kv.split('=') for kv in self.game['build'][0].split('\t')[2:] if '=' in kv)
        for d in (self.logs, self.stamps): d.mkdir(parents=True, exist_ok=True)

    # ---- stamps: a stage is skipped when its inputs are as at the last run
    def fresh(self, name, key):
        f = self.stamps / f'{name}.json'
        return f.is_file() and json.loads(f.read_text()).get('key') == key

    def stamp(self, name, key): (self.stamps / f'{name}.json').write_text(json.dumps({'key': key, 'time': time.strftime('%Y-%m-%d %H:%M:%S')}) + '\n')

    def env(self, **more):
        e = {'DM_GAMES_DIR': str(self.profile_root), 'DM_GAMES_FIRST': '1', 'DM_GAME': TITLE, 'GAME': TITLE, 'DM_ALLOW_OTHER_METAL': '1', 'NXNATIVE': str(NX),
             'AMPERE_LOWER': str(AMPERE), 'PYTHONDONTWRITEBYTECODE': '1'}
        d = dotnet_root()
        if d: e['DOTNET_ROOT'] = str(d); e['PATH'] = f'{d}:{os.environ.get("PATH", "")}'
        e.update(more); return e

    def corpus_dirs(self): return [d for d in (self.corpus / n for n in ('rdb', 'main-embedded', 'linkdata', 'dumps', 'mkw-bnsh')) if d.is_dir()]

    def chain_version(self):
        r = subprocess.run([PY, NX / 'dm/gfx/tools/dmsh_chain.py'], capture_output=True, text=True, cwd=NX / 'dm/gfx/tools')
        if r.returncode or not r.stdout.strip(): raise Fail(f'dmsh_chain.py gave no chain version: {r.stderr.strip()[-300:]}')
        return r.stdout.strip().splitlines()[-1]

    def lib(self): return self.cache / f'b-{self.chain_version()}'


def read_game_tsv(path):
    g = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.startswith('#'): continue
        k, _, v = line.partition('\t'); g.setdefault(k, []).append(v)
    return g


def build_id(nso):
    with open(nso, 'rb') as f:
        f.seek(0x40); return f.read(16).hex()


# ---------------------------------------------------------------- environment
def doctor(need_ios=True, quiet=False):
    """What this Mac has of what the build needs. Returns the list of problems (empty = ready)."""
    bad = []
    def ok(name, cond, hint):
        if not quiet: print(f'  {"ok     " if cond else "MISSING"}  {name}' + ('' if cond else f'   -> {hint}'))
        if not cond: bad.append(name)
    ok('macOS on Apple silicon', sys.platform == 'darwin' and platform.machine() == 'arm64', 'the host runs the game\'s arm64 code natively: an Apple silicon Mac is required')
    ok(f'Python 3.12+ (running {platform.python_version()})', sys.version_info >= (3, 12), 'brew install python@3.12, then run nxs2.py with python3.12')
    for mod, pip in (('capstone', 'capstone>=5,<6'), ('lz4', 'lz4'), ('fontTools', 'fonttools')):
        try: __import__(mod); have = True
        except ImportError: have = False
        ok(f'python module {mod}', have, f'{PY} -m pip install "{pip}"')
    try:
        import capstone; ok('capstone 5.x', capstone.__version__.startswith('5.'), f'{PY} -m pip install "capstone>=5,<6"')
    except ImportError: pass
    xc = subprocess.run(['xcode-select', '-p'], capture_output=True, text=True)
    ok('Xcode command line tools (xcode-select -p)', xc.returncode == 0 and xc.stdout.strip() != '', 'install Xcode from developer.apple.com, open it once, then: sudo xcode-select -s /Applications/Xcode.app')
    ok('clang', which('clang') is not None, 'comes with Xcode\'s command line tools')
    ok('make', which('make') is not None, 'comes with Xcode\'s command line tools')
    for sdk in ('macosx',) + (('iphoneos',) if need_ios else ()):
        r = subprocess.run(['xcrun', '-sdk', sdk, 'metal', '--version'], capture_output=True, text=True)
        ok(f'Metal compiler for {sdk}', r.returncode == 0, 'Xcode -> Settings -> Components: install the Metal toolchain' + (' and the iOS platform' if sdk == 'iphoneos' else ''))
    d = dotnet_root()
    have_rt = False
    if d:
        r = subprocess.run([str(d / 'dotnet'), '--list-runtimes'], capture_output=True, text=True)
        have_rt = any(l.startswith('Microsoft.NETCore.App 10.') for l in r.stdout.splitlines())
    ok('.NET 10 runtime (the shader translator)', have_rt, 'brew install --cask dotnet-sdk   (or the .NET 10 runtime from dotnet.microsoft.com)')
    if need_ios: ok('xcodegen (the iPhone build)', which('xcodegen', '~/.local/bin/xcodegen') is not None, 'brew install xcodegen')
    ok('git', which('git') is not None, 'comes with Xcode\'s command line tools')
    std = pathlib.Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    ok('a standard font on this Mac (Arial Unicode)', std.is_file(), 'STD_FONT=<a Unicode TTF> names another font for the game\'s text')
    return bad


def setup(args):
    """nxnative at the pinned commit with the Switch 2 patch and the rebuilt translator, and ampere-lower built."""
    bad = doctor(need_ios=not args.no_ios)
    if bad: raise Fail('install the missing tools above, then run setup again')
    if not (NX / '.git').is_dir():
        say(f'fetching nxnative ({UPSTREAM} at {UPSTREAM_COMMIT}) into {NX}')
        run(['git', 'clone', '--filter=blob:none', '--no-checkout', UPSTREAM, NX], HERE / 'setup-clone.log')
    def head(): return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, cwd=NX).stdout.strip()
    marker = NX / 'dm/gfx/nvn/.switch2-patch'
    patch = HERE / 'host/nxnative-switch2.patch'
    want = fingerprint(patch)
    if not head().startswith(UPSTREAM_COMMIT[:7]):
        say(f'checking out nxnative {UPSTREAM_COMMIT} (was {head() or "nothing"})')
        run(['git', 'checkout', '-q', '--', '.'], HERE / 'setup-reset.log', cwd=NX, ok=(0, 1))   # drop a patch applied to another commit
        if marker.exists(): marker.unlink()
        run(['git', 'fetch', 'origin', UPSTREAM_COMMIT], HERE / 'setup-fetch.log', cwd=NX, ok=(0, 128))
        run(['git', 'checkout', '-q', UPSTREAM_COMMIT], HERE / 'setup-checkout.log', cwd=NX)
    if not (marker.is_file() and marker.read_text().strip() == want):
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True, cwd=NX).stdout.strip()
        if dirty:
            say('resetting nxnative to the pinned commit before applying the patch')
            run(['git', 'checkout', '-q', '--', '.'], HERE / 'setup-reset.log', cwd=NX)
        say('applying the Switch 2 host patch')
        run(['git', 'apply', '--whitespace=nowarn', patch], HERE / 'setup-patch.log', cwd=NX)
        marker.write_text(want + '\n')
    bin_dst = NX / 'dm/gfx/tools/dmsh_xlate_bin'
    for f in sorted((HERE / 'host/dmsh_xlate_bin').iterdir()):
        dst = bin_dst / f.name
        if not dst.is_file() or dst.read_bytes() != f.read_bytes():
            shutil.copy2(f, dst); say(f'translator: {f.name} installed')
    (bin_dst / 'dmsh_xlate').chmod(0o755)
    if not AMPERE.is_file() or AMPERE.stat().st_mtime < (HERE / 'ampere-lower/main.cpp').stat().st_mtime:
        say('building ampere-lower (the Switch 2 shader lowering)')
        run(['sh', HERE / 'ampere-lower/build.sh'], HERE / 'setup-ampere.log')
    say(f'setup done: nxnative {head()} + Switch 2 patch at {NX}, ampere-lower at {AMPERE}')


# ---------------------------------------------------------------- the dump
def find_dump(args):
    """(exefs folder, romfs folder) of the update the recipe is for, under the folder given (the dump as a Switch 2 dumping tool writes it:
    <game>/updates/<version>/{exefs,romfs}, or a folder holding exefs/ and romfs/ directly), checked by the executable's build ID."""
    if args.exefs and args.romfs: cands = [(pathlib.Path(args.exefs), pathlib.Path(args.romfs))]
    else:
        d = pathlib.Path(args.dump).expanduser().resolve()
        roots = [d / 'updates' / str(VERSION), d]
        if (d / 'updates').is_dir(): roots += sorted((p for p in (d / 'updates').iterdir() if p.is_dir()), reverse=True)
        if (d / 'base').is_dir(): roots.append(d / 'base')
        cands = []
        for r in roots:
            for ex, ro in (('exefs', 'romfs'), ('ExeFS', 'RomFS'), ('exefs', 'RomFS'), ('ExeFS', 'romfs')):
                if (r / ex / 'main').is_file() and (r / ro).is_dir() and (r / ex, r / ro) not in cands: cands.append((r / ex, r / ro))
    if not cands: raise Fail(f'no dump under {args.dump}: expected <folder>/updates/{VERSION}/exefs/main and .../romfs/, or exefs/ and romfs/ in the folder itself')
    g = read_game_tsv(PROFILE_SRC / 'game.tsv')
    want = dict(kv.split('=') for kv in g['build'][0].split('\t')[2:] if '=' in kv)
    seen = []
    for ex, ro in cands:
        bid = build_id(ex / 'main'); seen.append(f'{ex}: main build {bid}')
        if bid == want['main']:
            for m, b in want.items():
                if (ex / m).is_file() and build_id(ex / m) != b: raise Fail(f'{ex / m} has build {build_id(ex / m)}, the recipe expects {b}')
                if not (ex / m).is_file(): raise Fail(f'{ex / m} is missing from the dump')
            if not any(ro.rglob('*')): raise Fail(f'{ro} is empty')
            return ex, ro
    raise Fail(f'this recipe is for {NAME} v{VERSION} (main build {want["main"]}); the dump holds:\n  ' + '\n  '.join(seen) +
               '\n(a base-game folder is not enough: the game\'s update is needed)')


# ---------------------------------------------------------------- stages
def st_profile(b):
    """The game profile the tools read: game.tsv, the entitlements and the empty shader tables (the package fills them later)."""
    b.profile.mkdir(parents=True, exist_ok=True)
    for name in ('game.tsv', 'pokopia.entitlements', 'runtime_keys.tsv', 'shader_sources.tsv', 'shader_facts.seed.tsv'):
        src = PROFILE_SRC / name
        if src.is_file(): shutil.copyfile(src, b.profile / name)
    for name, text in EMPTY_TABLES.items():
        if not (b.profile / name).is_file(): (b.profile / name).write_text(text)


def st_extract(b, exefs, romfs):
    """The game as nxnative's tools read it: the ExeFS copied, a control record with the game's name, the RomFS packed into one image."""
    b.exefs.mkdir(parents=True, exist_ok=True)
    for f in sorted(exefs.iterdir()):
        if f.is_file() and (not (b.exefs / f.name).is_file() or (b.exefs / f.name).stat().st_size != f.stat().st_size): shutil.copy2(f, b.exefs / f.name)
    nacp = b.extract / 'control.nacp'
    if not nacp.is_file(): nacp.write_bytes(make_nacp(NAME, int(TITLE, 16), '1.0.5'))
    say(f'RomFS image from {romfs} ({sum(1 for _ in romfs.rglob("*") if _.is_file())} files)')
    run([PY, NX / 'dm/tools/romfs_image.py', '--reuse', romfs, b.romfs_img], b.logs / 'romfs_image.log', env=b.env())
    if not b.romfs_img.is_file(): raise Fail('no RomFS image was written')


def make_nacp(name, title, version):   # a control record (NACP) with the game's name and version: Switch 2 dumps carry none, and the app build reads one
    import struct
    d = bytearray(0x4000)
    for lang in range(16):
        d[lang * 0x300:lang * 0x300 + len(name)] = name.encode(); d[lang * 0x300 + 0x200:lang * 0x300 + 0x207] = b'unknown'
    struct.pack_into('<Q', d, 0x3038, title); d[0x3060:0x3060 + len(version)] = version.encode()
    struct.pack_into('<Q', d, 0x3078, title); struct.pack_into('<Q', d, 0x30B0, title)
    return bytes(d)


def st_rewrite(b):
    """The game's code modules rewritten for Apple's kernel (nxnative's patch_nso.py), with their recipe tables."""
    key = fingerprint(*(b.exefs / m for m in b.modules), NX / 'dm/tools/patch_nso.py', PROFILE_SRC / 'game.tsv')
    if b.fresh('rewrite', key) and all((b.patched / f'{m}.patched.bin').is_file() for m in b.modules): return say('rewrite: up to date')
    b.patched.mkdir(parents=True, exist_ok=True)
    for m in b.modules:
        say(f'rewriting {m}')
        run([PY, NX / 'dm/tools/patch_nso.py', b.exefs / m, b.patched, m], b.logs / f'rewrite_{m}.log', env=b.env(), cwd=NX)
    b.stamp('rewrite', key)


def st_host(b, jobs):
    """The Mac host built for this game's profile (the profile is compiled in, so the binary is per game)."""
    srcs = [p for d in ('dm/host', 'dm/gfx', 'dm/gfx/nvn', 'dm/gfx/tex', 'dm/gfx/msl', 'dm/mac') for p in sorted((NX / d).iterdir())
            if p.is_file() and (p.suffix in ('.c', '.m', '.h', '.S', '.inc', '.metal') or p.name == 'Makefile')]   # the sources, not make's outputs
    key = fingerprint(*srcs, PROFILE_SRC / 'game.tsv')
    if b.fresh('host', key) and b.host.is_file(): return say('host: up to date')
    say('building the Mac host (dmhost)')
    run(['make', '-C', NX / 'dm/host', f'GAME={TITLE}', 'dmhost', f'-j{jobs}'], b.logs / 'host_make.log', env=b.env(), show=r'error:|\[RED\]')
    b.host.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(NX / 'dm/host/dmhost', b.host)
    b.stamp('host', key)


def st_font(b):
    """Stand-ins for the console's shared fonts, made from fonts on this Mac (nothing from the console): the standard font is a copy of the
    Mac's own Arial Unicode (STD_FONT=<ttf> names another), the button-icon font is generated by nxnative's make_ext_font.py."""
    b.font.mkdir(parents=True, exist_ok=True)
    std = pathlib.Path(os.environ.get('STD_FONT') or '/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    if not std.is_file(): raise Fail(f'no standard font at {std}: set STD_FONT=<a Unicode TTF on this Mac>')
    if not (b.font / 'standard.ttf').is_file(): shutil.copyfile(std, b.font / 'standard.ttf')
    if not (b.font / 'nintendo_ext.ttf').is_file():
        run([PY, NX / 'dm/tools/make_ext_font.py', b.font / 'nintendo_ext.ttf'], b.logs / 'make_ext_font.log', env=b.env())


def st_corpus(b, romfs, dumps):
    """The game's shaders as the lowering reads them: every shader container in the game's files (shader_sources.tsv says where, read straight from
    the dump's RomFS folder, read-only), and any shader dumps of earlier runs (--dumps). Writes <shader>/corpus/<set>/ and <shader>/corpus-ctl/."""
    if TITLE == '0400C3F00006E000':
        say('scanning all MKW RomFS shader containers (shader_split.py)')
        pkg = b.shader / 'native-split'; tables = b.shader / 'native-tables'
        if not (any(pkg.glob('*.bin')) and (tables / 'shader_map.tsv').is_file()):
            for d in (pkg, tables):
                if d.is_dir(): shutil.rmtree(d)
                d.mkdir(parents=True, exist_ok=True)
            run([PY, NX / 'dm/gfx/tools/shader_split.py', romfs, b.exefs / 'main', pkg,
                 tables / 'shader_map.tsv', tables / 'shader_facts.tsv', tables / 'shader_pairs.tsv'],
                b.logs / 'mkw_shader_split.log', env=b.env(), heartbeat=120)
        else: say('MKW native shader split: reusing completed package and map')
        run([PY, TOOLS / 'mkw_native_import.py', pkg, tables / 'shader_map.tsv', b.corpus, b.ctl],
            b.logs / 'mkw_shader_import.log', env=b.env())
        for name in ('shader_map.tsv', 'shader_facts.tsv', 'shader_pairs.tsv'):
            src = tables / name
            if src.is_file(): shutil.copy2(src, b.shader / f'native-{name}')
        say(f'MKW shader import produced {sum(1 for _ in b.corpus.glob("*.program.bin"))} programs and host keys')
        add_dumps(b, dumps)
        return
    sources = [l.split('\t') for l in (PROFILE_SRC / 'shader_sources.tsv').read_text().splitlines() if l.strip() and not l.startswith('#')]
    key = fingerprint(b.exefs / 'main', b.romfs_img, *sorted(TOOLS.glob('*.py')), sources)
    if not (b.fresh('corpus', key) and (b.ctl / 'hostkeys.tsv').is_file()):
        for d in (b.corpus, b.ctl):
            if d.is_dir(): shutil.rmtree(d)
        nso = b.shader / 'nso'; nso.mkdir(parents=True, exist_ok=True)
        say('reading the executable\'s data (nso_tool.py)')
        run([PY, TOOLS / 'nso_tool.py', b.exefs / 'main', '--out', nso], b.logs / 'nso_tool.log', env=b.env())
        jobs = []   # (label, corpus set, mode, args)
        for kind, *paths in sources:
            if kind == 'blob': jobs.append(('main', 'main-embedded', 'blob', [nso / 'main.rodata', 'main.rodata']))
            elif kind == 'linkdata': jobs.append(('linkdata', 'linkdata', 'linkdata', [romfs / p for p in paths]))
            elif kind == 'rdb': jobs.append((pathlib.Path(paths[0]).name, 'rdb', 'rdb', [romfs / paths[0]]))
        for label, cset, mode, args in jobs:
            for p in args:
                if isinstance(p, pathlib.Path) and not p.is_file(): raise Fail(f'shader source {p} is missing from the dump')
        say(f'extracting the shaders of {len(jobs)} sources')
        def extract(j):
            run([PY, TOOLS / 'glslc2_extract.py', b.corpus / j[1], j[2], *j[3]], b.logs / f'corpus_{j[0]}.log', env=b.env(), heartbeat=120); return j[0]
        with cf.ThreadPoolExecutor(max(1, min(4, os.cpu_count() or 2))) as ex:   # the extractions in parallel; the control scans one at a time (they append to one hostkeys.tsv)
            for label in ex.map(extract, jobs): say(f'  extracted {label}')
        for label, cset, mode, args in jobs:
            run([PY, TOOLS / 's2_ctl_scan.py', b.ctl, mode, *args], b.logs / f'ctl_{label}.log', env=b.env(), heartbeat=120)
        b.stamp('corpus', key)
    else: say('corpus: up to date')
    add_dumps(b, dumps)


def add_dumps(b, dumps):
    """Shader dumps recorded while the game ran (DM_NVN_SHDUMP folders: <key>.ctl + <key>.code) become corpus entries and host keys."""
    dirs = [pathlib.Path(d).expanduser() for d in dumps if pathlib.Path(d).expanduser().is_dir() and any(pathlib.Path(d).expanduser().glob('*.ctl'))]
    if not dirs: return
    say(f'adding shader dumps from {len(dirs)} folder(s)')
    run([PY, TOOLS / 's2_dumps.py', *dirs, '--corpus', b.corpus / 'dumps', '--ctl', b.ctl], b.logs / 's2_dumps.log', env=b.env(), show=r'new|added|programs')


def st_lower(b, jobs):
    """Every corpus program lowered to Maxwell with ampere-lower (incremental: programs lowered before are kept)."""
    say('lowering the shaders (ampere-lower)')
    run([PY, TOOLS / 'lower_corpus.py', b.maxwell, *b.corpus_dirs()], b.logs / 'lower.log', env=b.env(J=str(jobs)), show=r'^programs|already done|^done')


def st_package(b):
    """The shader package nxnative's chain reads, and the profile's shader tables (shader_map.tsv, shader_facts.tsv with the hand-made answers on top)."""
    say('packaging the shaders (s2_pkg.py)')
    for d in (b.pkg, b.tables):
        if d.is_dir(): shutil.rmtree(d)
    run([PY, TOOLS / 's2_pkg.py', b.ctl, b.maxwell, b.pkg, b.tables, *b.corpus_dirs()], b.logs / 's2_pkg.log', env=b.env(), show=r'records|keys|written|pkg')
    shutil.copyfile(b.tables / 'shader_map.tsv', b.profile / 'shader_map.tsv')
    if (b.tables / 'shader_pairs.tsv').is_file(): shutil.copyfile(b.tables / 'shader_pairs.tsv', b.profile / 'shader_pairs.tsv')
    facts = {}
    order = []
    header = []
    for line in (b.tables / 'shader_facts.tsv').read_text(encoding='utf-8').splitlines():
        if line.startswith('#'): header.append(line); continue
        if not line.strip(): continue
        k = line.split('\t')[0]
        if k not in facts: order.append(k)
        facts[k] = line
    seed = PROFILE_SRC / 'shader_facts.seed.tsv'
    n = 0
    if seed.is_file():
        for line in seed.read_text(encoding='utf-8').splitlines():
            if line.startswith('#') or not line.strip(): continue
            k = line.split('\t')[0]
            if k not in facts: order.append(k)
            facts[k] = line; n += 1
    (b.profile / 'shader_facts.tsv').write_text('\n'.join(header + [facts[k] for k in order]) + '\n', encoding='utf-8')
    say(f'tables: {len(order)} facts rows ({n} hand-made), shader map {sum(1 for l in (b.profile / "shader_map.tsv").read_text().splitlines() if l and not l.startswith("#")) - 1} entries')


def st_chain(b, ios):
    """nxnative's shader chain: the lowered programs to Metal libraries for the Mac (and the iPhone), in <shader>/cache/b-<chain>/ and <shader>/ios/b-<chain>/.
    Incremental through the chain's own cache; the first run compiles everything (around half an hour on an M-series Mac, more with the iOS libraries)."""
    say('Metal libraries (nxnative chain: translate, cross-compile, metal)' + (' for the Mac and the iPhone' if ios else ' for the Mac'))
    env = b.env(DMSH_S2_CS='1', DMSH_S2_CENSUS='1')
    if ios: env['IOSOUT'] = str(b.ios)
    b.cache.mkdir(parents=True, exist_ok=True)
    run([PY, NX / 'dm/gfx/tools/own_compilers.py', b.pkg, b.cache], b.logs / 'chain.log', env=env, cwd=NX / 'dm/gfx/tools', heartbeat=60,
        show=r'Metal cache|compiled|reused|\[RED\]|failed|success|lock')
    lib = b.lib()
    if not (lib / 'inputs.tsv').is_file(): raise Fail(f'the chain left no library stamp at {lib}/inputs.tsv; see {b.logs / "chain.log"}')


def st_mac_app(b):
    """A self-contained Mac app (nxnative's pack_app.py): launcher + host + the game files (APFS clones) + shader libraries + tables."""
    app = b.outputs / f'{NAME}.app'; b.outputs.mkdir(parents=True, exist_ok=True)
    envf = b.out / 'app_env.txt'; envf.write_text(APP_ENV)
    settings = b.out / 'launcher.json'
    run([PY, NX / 'dm/games/settings.py', TITLE, b.patched, b.extract / 'control.nacp', settings], b.logs / 'settings.log',
        env=b.env(GFXAPI='nvn', SETTINGS_PLATFORM='mac', DM_LANG='en-US'))
    cmd = [PY, NX / 'dm/mac/pack_app.py', app, '--host', b.host, '--patched', b.patched, '--romfs', b.romfs_img, '--msl', b.lib(), '--tables', b.profile,
           '--font', b.font, '--title-id', TITLE, '--name', NAME, '--bundle-id', f'org.nxnative.{TITLE.lower()}', '--lang', 'en-US', '--sign', '-',
           '--version', '1.0.5', '--category', b.game['app_category'][0], '--marker', b.game['romfs_marker'][0], '--env-file', envf, '--settings', settings]
    say('packing the Mac app')
    run(cmd, b.logs / 'pack_app.log', env=b.env(), show=r'^step:|^app:')
    if not (app / 'Contents/MacOS/dmhost').is_file(): raise Fail(f'no app at {app}')
    # the app's host records run-time shaders into shdump/ in its Application Support folder, which it does not create itself
    (pathlib.Path.home() / 'Library/Application Support/nxnative' / TITLE / 'shdump').mkdir(parents=True, exist_ok=True)
    say(f'Mac app: {app}')
    return app


def ipa_env(b):
    return b.env(W=str(b.out / 'ipa'), PATCHED=str(b.patched), ROMFS=str(b.romfs_img), MSL_IOS=str(b.ios), AOC='', DM_COPY_ROMFS='NO',
                 DM_ENTITLEMENTS=str(b.profile / 'pokopia.entitlements'), FONT_PY=PY, DM_SHOW_FPS='1', DM_PSO_GPUS='none', DM_ENV_FILE=str(b.out / 'phone_env.txt'),
                 PATH=os.path.expanduser('~/.local/bin') + ':' + b.env().get('PATH', os.environ.get('PATH', '')))


def st_ipa(b):
    """An unsigned IPA (nxnative's build_ipa.sh): the app without the game files, which the phone takes from Documents/romfs.img; the executable
    carries the two kernel entitlements ad-hoc, so a re-signer that keeps entitlements can sign it for your own device."""
    (b.out / 'phone_env.txt').write_text(PHONE_ENV)
    b.outputs.mkdir(parents=True, exist_ok=True)
    out = b.outputs / f'{NAME.replace(" ", "")}-unsigned.ipa'
    say('building the IPA (unsigned)')
    run(['bash', NX / 'dm/ios/tools/build_ipa.sh', out], b.logs / 'build_ipa.log', env=ipa_env(b), show=r'^\[\d\d:\d\d:\d\d\]|FAIL|error:')
    if not out.is_file(): raise Fail(f'no IPA at {out}; see {b.logs / "build_ipa.log"}')
    say(f'IPA: {out} ({out.stat().st_size // (1 << 20)} MiB; romfs.img goes to the app\'s Documents on the phone, see docs/IPHONE.md)')
    return out


def st_xcode(b, team):
    """An Xcode project for the game, to sign with your Apple developer team (nxs2.py sign, or Xcode itself)."""
    (b.out / 'phone_env.txt').write_text(PHONE_ENV)
    env = ipa_env(b); env['DM_TEAM_ID'] = team; env['DM_XCODE_PROJECT_DIR'] = str(b.out / 'xcode')
    say(f'preparing the Xcode project (team {team})')
    run(['bash', NX / 'dm/ios/tools/build_ipa.sh', '--prepare-xcode'], b.logs / 'prepare_xcode.log', env=env, show=r'^\[\d\d:\d\d:\d\d\]|FAIL|error:')
    proj = b.out / 'xcode' / 'DmIOS.xcodeproj'
    if not proj.is_dir(): raise Fail(f'no project at {proj}; see {b.logs / "prepare_xcode.log"}')
    say(f'Xcode project: {proj}')
    return proj


def sign(args):
    """Archive and export a development IPA from the prepared Xcode project with automatic signing (your Apple ID signed into Xcode, the
    iPhone's UDID on the team). The first run should happen in Terminal on the Mac itself: Xcode may ask for the keychain."""
    b = Build(args.build); team = args.team_id
    x = b.out / 'xcode'; d = b.out / 'build-ios'; ar = d / 'app.xcarchive'; ex = d / 'export'
    if not (x / 'DmIOS.xcodeproj').is_dir(): raise Fail(f'no Xcode project at {x}: run nxs2.py build ... --xcode --team-id {team} first')
    lst = subprocess.run(['xcodebuild', '-project', x / 'DmIOS.xcodeproj', '-list', '-json'], capture_output=True, text=True)
    schemes = json.loads(lst.stdout).get('project', {}).get('schemes', []) if lst.returncode == 0 else []
    scheme = next((s for s in schemes if TITLE in s), schemes[0] if schemes else None)
    if not scheme: raise Fail(f'no scheme in {x}: {lst.stderr.strip()[-300:]}')
    d.mkdir(parents=True, exist_ok=True)
    if ex.is_dir(): shutil.rmtree(ex)
    (d / 'export.plist').write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>method</key><string>{args.method}</string>
<key>teamID</key><string>{team}</string>
<key>signingStyle</key><string>automatic</string>
<key>destination</key><string>export</string>
<key>compileBitcode</key><false/>
<key>thinning</key><string>&lt;none&gt;</string>
<key>stripSwiftSymbols</key><true/>
</dict></plist>
''')
    say(f'archiving {scheme}')
    run(['xcodebuild', '-project', x / 'DmIOS.xcodeproj', '-scheme', scheme, '-configuration', 'Release', '-destination', 'generic/platform=iOS',
         '-archivePath', ar, '-allowProvisioningUpdates', 'archive'], b.logs / 'xcode_archive.log', cwd=x, heartbeat=20, show=r'error:|\*\* ARCHIVE')
    say('exporting the IPA')
    run(['xcodebuild', '-exportArchive', '-archivePath', ar, '-exportOptionsPlist', d / 'export.plist', '-exportPath', ex, '-allowProvisioningUpdates'],
        b.logs / 'xcode_export.log', cwd=x, heartbeat=20, show=r'error:|EXPORT')
    ipas = sorted(ex.glob('*.ipa'))
    if not ipas: raise Fail(f'the export wrote no IPA; see {b.logs / "xcode_export.log"}')
    b.outputs.mkdir(parents=True, exist_ok=True)
    out = b.outputs / f'{NAME.replace(" ", "")}-signed.ipa'; shutil.copyfile(ipas[0], out)
    say(f'signed IPA: {out}  (install: pymobiledevice3 apps install "{out}"; then docs/IPHONE.md for romfs.img)')


# ---------------------------------------------------------------- report
def coverage(b):
    """(file shaders in the map, runtime shaders known, runtime shaders present)"""
    known = {l.split('\t')[0] for l in (PROFILE_SRC / 'runtime_keys.tsv').read_text().splitlines() if l.strip() and not l.startswith('#')}
    keys = set()
    smap = b.profile / 'shader_map.tsv'
    if smap.is_file():
        for l in smap.read_text().splitlines():
            if l.startswith('#') or l.startswith('package\t') or not l.strip(): continue
            f = l.split('\t')
            if len(f) > 3: keys.add(f[3])
    return len(keys - known), len(known), len(keys & known)


def report(b):
    files, known, present = coverage(b)
    say(f'shaders: {files} from the game files; {present} of the {known} known run-time shaders')
    if present < known:
        print(f'''
  {known - present} shaders the game builds while it runs (opening, character creator, first area) are not in this build yet;
  a draw that needs one is skipped until it is. They are recorded on your device as you play
  (the apps have DM_NVN_SHDUMP on); then:   nxs2.py refresh "{b.out}"   adds them and rebuilds the outputs.
  (iPhone: pull the app's Documents/shdump first: tools/phone_pull.py, see docs/IPHONE.md)''')


def status(args):
    b = Build(args.build)
    for name, p in (('ExeFS', b.exefs / 'main'), ('RomFS image', b.romfs_img), ('rewritten modules', b.patched / 'main.patched.bin'), ('Mac host', b.host),
                    ('fonts', b.font / 'nintendo_ext.ttf'), ('shader corpus', b.ctl / 'hostkeys.tsv'), ('lowered shaders', b.maxwell / 'results.tsv'),
                    ('shader package', b.pkg), ('shader map', b.profile / 'shader_map.tsv')):
        print(f'  {"ok     " if p.exists() else "missing"}  {name}')
    try:
        lib = b.lib(); print(f'  {"ok     " if (lib / "inputs.tsv").is_file() else "missing"}  Mac shader libraries ({lib.name})')
        print(f'  {"ok     " if (b.ios / lib.name / "inputs.tsv").is_file() else "missing"}  iPhone shader libraries')
    except Fail as e: print(f'  ?        shader libraries: {e}')
    for p in sorted(b.outputs.glob('*')) if b.outputs.is_dir() else []: print(f'  output   {p}')
    if (b.profile / 'shader_map.tsv').is_file(): report(b)


# ---------------------------------------------------------------- commands
def build(args):
    need_ios = not args.no_ios
    bad = doctor(need_ios=need_ios, quiet=True)
    if bad: doctor(need_ios=need_ios); raise Fail('install the missing tools above (nxs2.py doctor), then build again')
    if not (NX / 'dm/host/Makefile').is_file() or not AMPERE.is_file(): raise Fail(f'run nxs2.py setup first (nxnative at {NX}, ampere-lower at {AMPERE})')
    exefs, romfs = find_dump(args)
    if args.out: out = pathlib.Path(args.out).expanduser()
    elif args.dump: d = pathlib.Path(args.dump).expanduser().resolve(); out = d.parent / f'{d.name}_native'   # beside the dump, never inside it
    else: raise Fail('--exefs/--romfs need --out <build folder>')
    b = Build(out); jobs = args.jobs or os.cpu_count() or 4
    say(f'{NAME} v{VERSION}: dump {exefs.parent}, build folder {b.out}')
    st_profile(b)
    st_extract(b, exefs, romfs)
    st_rewrite(b)
    st_host(b, jobs)
    st_font(b)
    st_corpus(b, romfs, args.dumps or [])
    st_lower(b, jobs)
    st_package(b)
    st_chain(b, need_ios)
    if args.mac_app: st_mac_app(b)
    if args.ipa: st_ipa(b)
    if args.xcode:
        if not args.team_id: raise Fail('--xcode needs --team-id <your Apple developer team ID>')
        st_xcode(b, args.team_id)
    if not (args.mac_app or args.ipa or args.xcode): say('no output asked for (--mac-app, --ipa, --xcode); the build folder is complete for nxs2.py run')
    report(b)
    say('done')


def default_dump_dirs(b):
    dirs = [b.rundir / 'shdump', pathlib.Path.home() / 'Library/Application Support/nxnative' / TITLE / 'shdump']
    if b.phone.is_dir(): dirs += sorted(p for p in b.phone.glob('*/shdump') if p.is_dir())
    return [d for d in dirs if d.is_dir()]


def refresh(args):
    b = Build(args.build)
    if not (b.ctl / 'hostkeys.tsv').is_file(): raise Fail(f'{b.out} holds no finished build')
    dirs = [pathlib.Path(d).expanduser() for d in args.dumps] or default_dump_dirs(b)
    if not dirs: raise Fail('no shader dumps found: give the folder(s) (DM_NVN_SHDUMP output: <key>.ctl + <key>.code files)')
    before = coverage(b)
    add_dumps(b, dirs)
    jobs = args.jobs or os.cpu_count() or 4
    st_lower(b, jobs); st_package(b)
    st_chain(b, (b.ios / b.lib().name / 'inputs.tsv').is_file())   # iPhone libraries again when the build had them
    if (b.outputs / f'{NAME}.app').is_dir(): st_mac_app(b)
    if b.outputs.is_dir() and any(b.outputs.glob('*-unsigned.ipa')): st_ipa(b)
    if (b.out / 'xcode' / 'DmIOS.xcodeproj').is_dir():
        team = args.team_id
        if not team and (b.out / 'xcode' / 'project.yml').is_file():
            m = re.search(r'DEVELOPMENT_TEAM\s*[:=]\s*"?([A-Z0-9]{10})', (b.out / 'xcode' / 'project.yml').read_text()); team = m.group(1) if m else None
        if team: st_xcode(b, team); say(f'the Xcode project is refreshed: nxs2.py sign "{b.out}" --team-id {team} makes the new signed IPA')
        else: say('the Xcode project is not refreshed (give --team-id)')
    after = coverage(b)
    say(f'run-time shaders: {before[2]} -> {after[2]} of {after[1]} known' + (f', plus {after[0] - before[0]} not in the known list' if after[0] != before[0] else ''))
    report(b); say('done')


def run_mac(args):
    """The game in a window on this Mac, straight from the build folder (the host, not the app): saves in <build>/run/save, dumps in <build>/run/shdump."""
    b = Build(args.build)
    for p in (b.host, b.romfs_img, b.patched / 'main.patched.bin'):
        if not p.exists(): raise Fail(f'{p} is missing: run nxs2.py build first')
    rd = b.rundir
    for d in ('save', 'temp', 'shdump'): (rd / d).mkdir(parents=True, exist_ok=True)   # the host writes dumps only into a folder that exists
    env = dict(os.environ, DM_ROOT=str(b.out), DM_ROMFS=str(b.romfs_img), DM_SAVE=str(rd / 'save'), DM_TEMP=str(rd / 'temp'), DM_MSL_DIR=str(b.cache),
               DM_FONT_DIR=str(b.font), DM_NVN_SHMAP=str(b.profile / 'shader_map.tsv'), DM_LANG=args.lang, DM_WINDOW='1', DM_AUDIO='1', DM_NVN_SHDUMP='shdump')
    for kv in args.env: k, _, v = kv.partition('='); env[k] = v
    say(f'starting {NAME} (window; Ctrl-C here or close the window to stop; log {rd / "host.log"})')
    with open(rd / 'host.log', 'w') as log:
        try: rc = subprocess.call([str(b.host), str(b.patched)], env=env, cwd=rd, stdout=log, stderr=subprocess.STDOUT)
        except KeyboardInterrupt: rc = 130
    n = len(list((rd / 'shdump').glob('*.ctl'))) if (rd / 'shdump').is_dir() else 0
    say(f'host exited ({rc}); {n} shaders recorded in {rd / "shdump"}' + (f'  ->  nxs2.py refresh "{b.out}"' if n else ''))


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('-v', '--verbose', action='store_true', help='show every line the tools print (default: a heartbeat and the important lines; everything goes to <build>/logs)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('setup'); s.add_argument('--no-ios', action='store_true'); s.set_defaults(f=setup)
    s = sub.add_parser('doctor'); s.add_argument('--no-ios', action='store_true'); s.set_defaults(f=lambda a: sys.exit(1 if doctor(not a.no_ios) else 0))
    s = sub.add_parser('build')
    s.add_argument('dump', nargs='?', help='the dump folder (<game>/updates/<version>/{exefs,romfs} inside, or exefs/ and romfs/ directly)')
    s.add_argument('--exefs'); s.add_argument('--romfs'); s.add_argument('--out', help=f'the build folder (default: <dump>/../{TITLE}_native)')
    s.add_argument('--mac-app', action='store_true'); s.add_argument('--ipa', action='store_true'); s.add_argument('--xcode', action='store_true')
    s.add_argument('--team-id'); s.add_argument('--dumps', nargs='*', default=[]); s.add_argument('--no-ios', action='store_true'); s.add_argument('-j', '--jobs', type=int)
    s.set_defaults(f=build)
    s = sub.add_parser('refresh'); s.add_argument('build'); s.add_argument('dumps', nargs='*'); s.add_argument('--team-id'); s.add_argument('-j', '--jobs', type=int); s.set_defaults(f=refresh)
    s = sub.add_parser('run'); s.add_argument('build'); s.add_argument('--lang', default='en-US'); s.add_argument('env', nargs='*', help='extra DM_* switches, KEY=VALUE'); s.set_defaults(f=run_mac)
    s = sub.add_parser('sign'); s.add_argument('build'); s.add_argument('--team-id', required=True); s.add_argument('--method', default='debugging', help='export method: debugging (default), ad-hoc, release-testing'); s.set_defaults(f=sign)
    s = sub.add_parser('status'); s.add_argument('build'); s.set_defaults(f=status)
    a = ap.parse_args(); VERBOSE = a.verbose
    if a.cmd == 'build' and not (a.dump or (a.exefs and a.romfs)): ap.error('build needs the dump folder, or --exefs and --romfs')
    try: a.f(a)
    except Fail as e: print(f'\n[FAIL] {e}', file=sys.stderr); sys.exit(1)
    except KeyboardInterrupt: print('\ninterrupted', file=sys.stderr); sys.exit(130)


VERBOSE = False
if __name__ == '__main__': main()
