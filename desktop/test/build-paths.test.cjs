const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { spawnSync } = require('node:child_process');

const repo = path.resolve(__dirname, '../..');
const script = path.join(__dirname, '../scripts/strip-build-paths.py');
const uv = spawnSync('uv', ['--version'], { windowsHide: true, encoding: 'utf8' });

// The runtime is assembled inside a checkout and installed on other machines (§2.7):
// whatever remembers the checkout is removed, and what cannot be removed stops the build.
test('bundled runtime keeps no path of the machine that built it',
  { skip: uv.error?.code === 'ENOENT' ? 'uv is required to run the build script' : false, timeout: 120_000 }, () => {
    const scratch = path.join(repo, '.scratch');
    fs.mkdirSync(scratch, { recursive: true });
    const fixture = fs.mkdtempSync(path.join(scratch, 'build-paths-'));
    const runtime = path.join(fixture, 'runtime');
    function write(relative, content) {
      const target = path.join(runtime, relative);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, content);
    }
    const exists = relative => fs.existsSync(path.join(runtime, relative));
    const files = () => fs.readdirSync(runtime, { recursive: true })
      .filter(relative => fs.statSync(path.join(runtime, relative)).isFile());
    function strip() {
      const result = spawnSync('uv', [
        'run', '--no-project', '--no-python-downloads', 'python', '-I', '-B', script, runtime, fixture,
      ], { cwd: fixture, windowsHide: true, encoding: 'utf8', timeout: 90_000 });
      if (result.error) throw result.error;
      return result;
    }
    try {
      write('python/Lib/stdlib_module.py', 'VALUE = 1\n');
      write('python/Lib/site-packages/pkg/__init__.py', 'VALUE = 2\n');
      write('python/Lib/site-packages/pkg/template.py', 'this is {{ not }} python\n');
      write('python/Lib/site-packages/pkg-1.0.dist-info/direct_url.json',
        JSON.stringify({ url: pathToFileURL(path.join(fixture, 'backend')).href, dir_info: {} }));
      write('python/Lib/site-packages/other-1.0.dist-info/direct_url.json',
        JSON.stringify({ url: 'https://example.invalid/other-1.0.whl', archive_info: {} }));
      write('python/Scripts/pkg.exe', `MZ launcher\0${path.join(runtime, 'python', 'python.exe')}\0`);
      write('python/Scripts/uv.exe', 'MZ a real executable that names no interpreter');

      const clean = strip();
      assert.equal(clean.status, 0, `${clean.stdout}\n${clean.stderr}`);
      assert.ok(!exists('python/Lib/site-packages/pkg-1.0.dist-info/direct_url.json'),
        'A local source directory must not be recorded');
      assert.ok(exists('python/Lib/site-packages/other-1.0.dist-info/direct_url.json'),
        'A download URL names no machine and stays');
      assert.ok(!exists('python/Scripts/pkg.exe'), 'A launcher bound to the build interpreter must go');
      assert.ok(exists('python/Scripts/uv.exe'), 'A real executable must stay');

      const compiled = files().filter(relative => relative.endsWith('.pyc'));
      assert.deepEqual(compiled.map(relative => path.basename(relative).split('.')[0]).sort(),
        ['__init__', 'stdlib_module'], 'Sources compile; a file that is not Python is skipped, not fatal');
      const recorded = path.join('Lib', 'site-packages', 'pkg', '__init__.py');
      assert.ok(fs.readFileSync(path.join(runtime, compiled.find(relative => relative.includes('__init__'))))
        .includes(recorded), `Bytecode must record its source as ${recorded}`);
      for (const relative of files()) {
        assert.ok(!fs.readFileSync(path.join(runtime, relative)).toString('latin1').toLowerCase()
          .includes(fixture.toLowerCase()), `${relative} still names the build directory`);
      }

      // Nothing knows how to clean these, so the build must stop rather than ship them:
      // JSON doubles the backslashes, and a binary may hold the path in UTF-16 and any case.
      write('node/notes.json', JSON.stringify({ built_in: fixture }));
      write('node/blob.bin', Buffer.from(fixture.toUpperCase(), 'utf16le'));
      const dirty = strip();
      assert.equal(dirty.status, 1, `${dirty.stdout}\n${dirty.stderr}`);
      assert.match(dirty.stderr, /notes\.json/);
      assert.match(dirty.stderr, /blob\.bin/);
    } finally {
      const relative = path.relative(scratch, fixture);
      assert.ok(relative.startsWith('build-paths-') && !relative.includes(path.sep) && !path.isAbsolute(relative),
        'Refusing to remove a fixture outside the scratch directory');
      fs.rmSync(fixture, { recursive: true, force: true });
    }
  });
