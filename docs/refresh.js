// Read the newest published source from the repository, bypassing Pages and browser caches.
(() => {
  const button = document.querySelector('[data-refresh]');
  if (!button) return;
  button.addEventListener('click', async () => {
    const filename = location.pathname.split('/').pop() || 'index.html';
    if (!/^(index|market|watchlist|stocks|sectors|disclosures|signals|portfolio)\.html$/.test(filename)) return;
    const oldText = button.textContent;
    button.disabled = true;
    button.textContent = '확인 중…';
    try {
      const url = `https://raw.githubusercontent.com/gptjyp-ops/AI-stock-orchestrator/main/docs/${filename}?t=${Date.now()}`;
      const response = await fetch(url, {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const markup = await response.text();
      const parsed = new DOMParser().parseFromString(markup, 'text/html');
      const current = document.querySelector('footer')?.textContent;
      const latest = parsed.querySelector('footer')?.textContent;
      if (!latest || !parsed.querySelector('main')) throw new Error('올바르지 않은 응답');
      if (current === latest) {
        button.textContent = '이미 최신 업로드본입니다';
      } else {
        document.open();
        document.write(markup);
        document.close();
      }
    } catch (error) {
      button.textContent = '확인 실패 · 다시 시도';
      console.error('Latest upload check failed:', error);
    } finally {
      button.disabled = false;
      if (button.isConnected) setTimeout(() => { button.textContent = oldText; }, 3000);
    }
  });
})();
