import { isAbsolute } from 'node:path';

// Sandboxed hosts can authorize a checkout owned by the interactive user through
// protected process configuration. Keep only concrete trust paths, never an
// inherited helper, header, include, rewrite, or wildcard ownership exemption.
export function gitConfiguration(settings = [], environment = process.env) {
  const count = /^\d{1,4}$/.test(environment.GIT_CONFIG_COUNT || '') ? Number(environment.GIT_CONFIG_COUNT) : 0;
  const trusted = [];
  if (count <= 1024) for (let index = 0; index < count; index++) {
    const key = environment[`GIT_CONFIG_KEY_${index}`];
    const value = environment[`GIT_CONFIG_VALUE_${index}`];
    if (typeof key === 'string' && key.toLowerCase() === 'safe.directory' && typeof value === 'string'
        && (value === '' || isAbsolute(value)) && !/[\x00-\x1f*]/.test(value)) trusted.push(['safe.directory', value]);
  }
  const entries = [...trusted, ...settings];
  if (!entries.length) return {};
  return Object.assign({ GIT_CONFIG_COUNT: String(entries.length) }, ...entries.map(([key, value], index) => ({
    [`GIT_CONFIG_KEY_${index}`]: key, [`GIT_CONFIG_VALUE_${index}`]: value,
  })));
}
