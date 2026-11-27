export function showUiIds(search = location.search) {
  if (new URLSearchParams(search).get('ui') !== 'ids') return;

  const overlay = document.createElement('div');
  overlay.className = 'ui-ids-overlay';
  overlay.setAttribute('aria-hidden', 'true');
  document.body.append(overlay);

  let frame;
  const update = () => {
    frame = undefined;
    const labels = document.createDocumentFragment();
    const positions = [];
    for (const element of document.querySelectorAll('[data-ui]')) {
      const { top, right, bottom, left, width, height } = element.getBoundingClientRect();
      if (!width || !height || bottom < 0 || right < 0 || top > innerHeight || left > innerWidth ||
          element.closest('[inert]') || getComputedStyle(element).visibility === 'hidden') continue;

      const label = document.createElement('span');
      label.textContent = element.dataset.ui;
      positions.push({ label, top, right });
      labels.append(label);
    }
    overlay.replaceChildren(labels);
    const placed = [];
    for (const { label, top, right } of positions) {
      const { offsetWidth: width, offsetHeight: height } = label;
      const x = Math.max(0, Math.min(right - width, innerWidth - width));
      let y = Math.max(0, top - 1);
      const overlap = other => x < other.right && x + width > other.left && y < other.bottom && y + height > other.top;
      for (let other; (other = placed.find(overlap));) y = other.bottom + 1;
      placed.push({ left: x, right: x + width, top: y, bottom: y + height });
    }
    positions.forEach(({ label }, index) => {
      label.style.left = placed[index].left + 'px';
      label.style.top = placed[index].top + 'px';
    });
  };
  const schedule = () => { frame ??= requestAnimationFrame(update); };
  const observer = new MutationObserver(changes => {
    if (overlay.isConnected && changes.some(change => !overlay.contains(change.target))) schedule();
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
  window.addEventListener('scroll', schedule, { capture: true, passive: true });
  window.addEventListener('resize', schedule);
  document.addEventListener('toggle', schedule, true);
  schedule();
}
