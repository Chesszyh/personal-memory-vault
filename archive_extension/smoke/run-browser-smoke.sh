#!/usr/bin/env bash
set -euo pipefail

pmv_smoke_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
pmv_extension_dir="$(cd -- "${pmv_smoke_script_dir}/.." && pwd -P)"
pmv_expected_id="ainoobmdpanhopangobnggdkpljnpmgl"

if [[ -n "${PMV_CHROME_BIN:-}" ]]; then
  pmv_chrome_bin="${PMV_CHROME_BIN}"
else
  pmv_chrome_bin=""
  for pmv_candidate in chromium chromium-browser; do
    if command -v "${pmv_candidate}" >/dev/null 2>&1; then
      pmv_chrome_bin="$(command -v "${pmv_candidate}")"
      break
    fi
  done
  if [[ -z "${pmv_chrome_bin}" ]]; then
    pmv_chrome_bin="$(
      fd --type f --max-depth 3 '^chrome$' "${HOME}/.cache/ms-playwright" 2>/dev/null \
        | sort -V \
        | tail -n 1
    )"
  fi
  if [[ -z "${pmv_chrome_bin}" ]]; then
    for pmv_candidate in google-chrome-stable google-chrome; do
      if command -v "${pmv_candidate}" >/dev/null 2>&1; then
        pmv_chrome_bin="$(command -v "${pmv_candidate}")"
        break
      fi
    done
  fi
fi

if [[ -z "${pmv_chrome_bin}" || ! -x "${pmv_chrome_bin}" ]]; then
  echo "browser smoke blocked: no Chrome/Chromium executable found" >&2
  exit 2
fi
if ! command -v bwrap >/dev/null 2>&1; then
  echo "browser smoke blocked: bwrap is required for per-process network isolation" >&2
  exit 2
fi
if ! /usr/bin/python -c 'import playwright.sync_api' >/dev/null 2>&1; then
  echo "browser smoke blocked: Python Playwright is required" >&2
  exit 2
fi
pmv_playwright_site="$(
  /usr/bin/python -c \
    'from pathlib import Path; import playwright; print(Path(playwright.__file__).parent.parent)'
)"

pmv_chrome_version="$(${pmv_chrome_bin} --version)"
if [[ "${pmv_chrome_version}" == Google\ Chrome\ * && "${pmv_chrome_version}" != Google\ Chrome\ for\ Testing\ * ]]; then
  echo "browser smoke blocked: Google Chrome 137+ ignores --load-extension" >&2
  echo "use unbranded Chromium or Chrome for Testing via PMV_CHROME_BIN" >&2
  exit 2
fi

pmv_smoke_root="$(mktemp -d /tmp/pmv-archive-extension-smoke.XXXXXXXX)"
pmv_profile_dir="${pmv_smoke_root}/profile"
pmv_home_dir="${pmv_smoke_root}/home"
pmv_cache_dir="${pmv_smoke_root}/cache"
pmv_runtime_dir="${pmv_smoke_root}/runtime"
pmv_tmp_dir="${pmv_smoke_root}/tmp"
pmv_result_output="${pmv_smoke_root}/smoke-result.json"

mkdir -p -- \
  "${pmv_profile_dir}" \
  "${pmv_home_dir}" \
  "${pmv_cache_dir}" \
  "${pmv_runtime_dir}" \
  "${pmv_tmp_dir}"
chmod 700 "${pmv_runtime_dir}"

pmv_cleanup() {
  if [[ "${PMV_KEEP_SMOKE:-0}" == "1" ]]; then
    echo "browser smoke artifacts kept at ${pmv_smoke_root}" >&2
    return
  fi
  case "${pmv_smoke_root}" in
    /tmp/pmv-archive-extension-smoke.*)
      rm -rf -- "${pmv_smoke_root}"
      ;;
    *)
      echo "refusing to remove unexpected smoke path: ${pmv_smoke_root}" >&2
      ;;
  esac
}
trap pmv_cleanup EXIT

if ! timeout --signal=TERM --kill-after=5s 90s bwrap \
  --die-with-parent \
  --new-session \
  --unshare-net \
  --ro-bind / / \
  --bind "${pmv_smoke_root}" "${pmv_smoke_root}" \
  --dev-bind /dev /dev \
  --proc /proc \
  --setenv HOME "${pmv_home_dir}" \
  --setenv XDG_CONFIG_HOME "${pmv_home_dir}/.config" \
  --setenv XDG_CACHE_HOME "${pmv_cache_dir}" \
  --setenv XDG_RUNTIME_DIR "${pmv_runtime_dir}" \
  --setenv TMPDIR "${pmv_tmp_dir}" \
  --setenv PYTHONPATH "${pmv_playwright_site}" \
  /usr/bin/python \
  "${pmv_smoke_script_dir}/run_browser_smoke.py" \
  --chrome "${pmv_chrome_bin}" \
  --extension "${pmv_extension_dir}" \
  --profile "${pmv_profile_dir}" \
  --extension-id "${pmv_expected_id}" \
  >"${pmv_result_output}"; then
  echo "browser smoke failed or timed out" >&2
  sed -n '1,80p' "${pmv_result_output}" >&2
  exit 1
fi

if jq -e '.status == "pass"' "${pmv_result_output}" >/dev/null; then
  echo "browser smoke PASS"
  echo "browser: ${pmv_chrome_version}"
  echo "extension id: ${pmv_expected_id}"
  echo "isolation: bwrap --unshare-net, fresh temporary profile, read-only host filesystem"
  jq -r '.checks[] | "- " + .' "${pmv_result_output}"
  exit 0
fi

echo "browser smoke FAIL: smoke page did not report pass" >&2
sed -n '1,80p' "${pmv_result_output}" >&2
exit 1
