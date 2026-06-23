export function renderMarkdown(text) {
  const unsafeHtml = window.marked.parse(text || '', { breaks: true, gfm: true });
  const safeHtml = window.DOMPurify.sanitize(unsafeHtml);
  setTimeout(() => {
    if (window.hljs) {
      document.querySelectorAll('.msg-content pre code').forEach((block) => {
        window.hljs.highlightElement(block);
      });
    }
  }, 0);
  return safeHtml;
}
