const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const repo = path.resolve(__dirname, '../..');
const uv = spawnSync('uv', ['--version'], { windowsHide: true, encoding: 'utf8' });

// Exercise the real packaging configuration: an editable install never encounters
// the ancestor .gitignore that excluded the backend from the desktop wheel (§2.7).
test('runtime wheel ships backend, generated SPA and built-in plugins despite repository ignores',
  { skip: uv.error?.code === 'ENOENT' ? 'uv is required to build the desktop runtime' : false, timeout: 120_000 }, () => {
    const scratch = path.join(repo, '.scratch');
    fs.mkdirSync(scratch, { recursive: true });
    const fixture = fs.mkdtempSync(path.join(scratch, 'runtime-wheel-'));
    const backend = path.join(fixture, 'backend');
    function write(relative, content) {
      const target = path.join(fixture, relative);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, content);
    }
    function run(args) {
      const result = spawnSync('uv', args, {
        cwd: fixture, windowsHide: true, encoding: 'utf8', timeout: 90_000,
      });
      if (result.error) throw result.error;
      assert.equal(result.status, 0, `uv ${args.join(' ')}\n${result.stdout}\n${result.stderr}`);
      return result.stdout;
    }
    try {
      for (const relative of [
        'CHANGELOG.md', 'backend/README.md', 'backend/pyproject.toml',
        'backend/hatch_build.py', 'backend/voxarium_studio/__init__.py',
        'backend/voxarium_studio/build_info.py', 'backend/voxarium_studio/main.py',
        'backend/voxarium_studio/cli.py', 'backend/voxarium_studio/core/__init__.py',
        'backend/voxarium_studio/core/project.py',
      ]) write(relative, fs.readFileSync(path.join(repo, relative)));
      // Hatch disregards all VCS patterns when the build root itself is ignored.
      // The fixture lives in scratch, but must retain the checkout's other rules.
      write('.gitignore', fs.readFileSync(path.join(repo, '.gitignore'), 'utf8').replace(/^\.scratch\/\r?$/m, ''));
      write('backend/voxarium_studio/static/index.html', '<script src="assets/app.js"></script>');
      write('backend/voxarium_studio/static/assets/app.js', 'console.log("packaged SPA");');
      write('backend/plugins_builtin/example/plugin.yaml', 'id: example\n');
      write('backend/plugins_builtin/example/plugin.py', 'PLUGIN = "example"\n');
      write('backend/voxarium_studio/__pycache__/main.cpython-312.pyc', 'stale bytecode');
      write('backend/voxarium_studio/main.pyc', 'stale bytecode');
      write('backend/voxarium_studio/_build_stamp.json', '{"commit":"stale-checkout-stamp"}');
      write('voxarium_studio/private-project-data.txt', 'managed project data');

      const output = path.join(fixture, 'dist');
      run(['build', '--wheel', '--out-dir', output, backend]);
      const wheels = fs.readdirSync(output).filter(name => name.endsWith('.whl'));
      assert.equal(wheels.length, 1);
      const entries = JSON.parse(run([
        'run', '--no-project', '--no-python-downloads', 'python', '-c',
        'import json, sys, zipfile; print(json.dumps(zipfile.ZipFile(sys.argv[1]).namelist()))',
        path.join(output, wheels[0]),
      ]));
      for (const required of [
        '__init__.py', 'main.py', 'cli.py', 'core/project.py',
        'static/index.html', 'static/assets/app.js',
        'plugins_builtin/example/plugin.yaml', 'plugins_builtin/example/plugin.py', 'CHANGELOG.md',
      ]) assert.ok(entries.includes(`voxarium_studio/${required}`), `Wheel is missing ${required}`);
      assert.ok(entries.every(entry => !entry.includes('__pycache__') && !/\.py[co]$/.test(entry)),
        'Wheel must not contain stale bytecode');
      assert.ok(!entries.includes('voxarium_studio/_build_stamp.json'),
        'A stale source-tree stamp must not ship when the build hook has no current commit');
      assert.ok(!entries.includes('voxarium_studio/private-project-data.txt'),
        'Managed project data must stay outside the application package');
    } finally {
      const relative = path.relative(scratch, fixture);
      assert.ok(relative.startsWith('runtime-wheel-') && !relative.includes(path.sep) && !path.isAbsolute(relative),
        'Refusing to remove a fixture outside the scratch directory');
      fs.rmSync(fixture, { recursive: true, force: true });
    }
  });
