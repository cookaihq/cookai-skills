// Trusted packaged renderer. Business/user data is inserted as text, never executable HTML.
export function register(add) {
  add('receipt', (container, data) => {
    const article = document.createElement('article');
    article.className = 'prose';
    for (const [tag, text] of [['h2',data.status],['p',data.message],['p',data.reference]]) {
      const node = document.createElement(tag); node.textContent = text; article.append(node);
    }
    container.replaceChildren(article);
  });
}
