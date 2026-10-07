import { NAVIGATION_NOTICE } from "../protocol";

export interface ArtifactDocument { html: string; policy: string }

function escapeAttribute(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/**
 * The artifact runs in an inner frame of a trusted wrapper. The wrapper's CSP (frame-src 'none')
 * governs every navigation the artifact frame attempts, so it cannot leave for another site.
 * The wrapper restores the artifact and tells the panel when a navigation was blocked.
 * The standalone console gets the same wrapper by URL from the service (app/render.py wrapper_document).
 */
export function wrapperDocument(document: ArtifactDocument): string {
  return `<!doctype html><meta http-equiv="Content-Security-Policy" content="${escapeAttribute(document.policy)}">`
    + "<style>html,body{margin:0;height:100%;overflow:hidden;background:transparent}"
    + "iframe{display:block;border:0;width:100%;height:100%;background:#fff}</style>"
    + `<iframe sandbox="allow-scripts" referrerpolicy="no-referrer" srcdoc="${escapeAttribute(document.html)}"></iframe>`
    + "<script>(function(){var f=document.querySelector('iframe'),n=0;f.addEventListener('load',function(){"
    + `if(++n>1){n=0;f.srcdoc=f.srcdoc;parent.postMessage("${NAVIGATION_NOTICE}","*");}});})();</script>`;
}
