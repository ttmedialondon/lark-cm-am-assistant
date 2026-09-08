/* Read-only page: filter server-rendered account rows without sending or storing data. */
(() => {
  "use strict";
  const search = document.getElementById("review-search");
  const filter = document.getElementById("review-filter");
  if (!search || !filter) return;
  const rows = [...document.querySelectorAll("tr[data-review-account]")].map(row => ({
    row,
    text: [...row.querySelectorAll("[data-search-text]")]
      .map(node => node.textContent).join(" ").toLocaleLowerCase(),
  }));
  const categories = new Set(["documented", "archive_needed", "decision_needed"]);

  function applyFilters() {
    const query = search.value.trim().replace(/^@+/, "").toLocaleLowerCase();
    const state = filter.value;
    let visible = 0;
    for (const {row, text} of rows) {
      const matchesState = state === "all"
        || (state === "stale" ? row.dataset.stale === "true"
          : categories.has(state) ? row.dataset.category === state : row.dataset.issue === state);
      row.hidden = !(text.includes(query) && matchesState);
      if (!row.hidden) visible += 1;
    }
    document.getElementById("review-visible-count").textContent = `${visible} / ${rows.length} 个账号`;
    document.getElementById("review-empty").hidden = visible !== 0;
  }

  search.addEventListener("input", applyFilters);
  filter.addEventListener("change", applyFilters);
  document.getElementById("review-clear-filters").addEventListener("click", () => {
    search.value = "";
    filter.value = "all";
    applyFilters();
    search.focus();
  });
  applyFilters();
})();
