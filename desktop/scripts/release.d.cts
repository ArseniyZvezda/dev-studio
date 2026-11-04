export interface ReleaseInfo {
  display: string;
  version: string;
  installerVersion: string;
}
export function sourceReleaseInfo(root: string, fallbackVersion: string): ReleaseInfo;
export function releaseInfo(stamp: {
  version: string;
  tag?: string;
  short_commit?: string;
  commit?: string;
}, fallbackVersion?: string): ReleaseInfo;
