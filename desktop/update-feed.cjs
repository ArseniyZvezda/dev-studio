// Build and runtime use one release-feed policy (dev spec §12.1).
function updateFeedOptions(env = process.env) {
  const repository = env.VOXARIUM_STUDIO_UPDATE_REPO || 'voxarium-studio/dev-studio';
  const match = /^([a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?)\/([a-z0-9_.-]{1,100})$/i.exec(repository);
  if (!match || match[0] !== repository || ['.', '..'].includes(match[2])) {
    throw new Error('Expected owner/repository for update feed');
  }
  return { provider: 'github', owner: match[1], repo: match[2],
    private: env.VOXARIUM_STUDIO_UPDATE_PRIVATE === '1' };
}

function githubCliToken() {
  return new Promise(resolve => {
    require('node:child_process').execFile('gh', ['auth', 'token', '--hostname', 'github.com'],
      { windowsHide: true, timeout: 10_000 }, (error, stdout) => resolve(error ? '' : stdout.trim()));
  });
}

async function configureUpdateFeed({ updater, env = process.env, readCliToken = githubCliToken,
  accessError = 'Private update feed requires GitHub credentials' }) {
  const feed = updateFeedOptions(env);
  if (!feed) return;
  // Public releases never inspect personal credentials. The explicit false also keeps
  // electron-updater from choosing its private provider when GH_TOKEN happens to exist.
  if (feed.private) {
    const token = env.GH_TOKEN || env.GITHUB_TOKEN || await readCliToken();
    if (!token) throw new Error(accessError);
    feed.token = token;
  }
  updater.setFeedURL(feed);
}

module.exports = { updateFeedOptions, configureUpdateFeed };
