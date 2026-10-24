// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Hand a file the server sent to the browser's own download. The file is
 * fetched as a blob rather than linked, so a download can stay behind a
 * session and a CSRF-checked POST.
 */
export function saveBlob(blob, fileName) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
