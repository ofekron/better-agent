(function registerMarketplaceCatalog(root) {
  function marketplaceRows(catalog, installed, query = "") {
    const rows = Array.isArray(catalog) ? [...catalog] : [];
    const catalogIds = new Set(rows.map((item) => item?.id).filter(Boolean));
    const needle = String(query || "").trim().toLowerCase();
    for (const record of Array.isArray(installed) ? installed : []) {
      const manifest = record?.manifest;
      if (!manifest?.id || catalogIds.has(manifest.id)) continue;
      const haystack = `${manifest.id} ${manifest.name || ""} ${manifest.description || ""}`.toLowerCase();
      if (!needle || haystack.includes(needle)) rows.push(manifest);
    }
    return rows;
  }

  function isMarketplaceManaged(record) {
    return record?.source?.type === "marketplace";
  }

  root.marketplaceCatalog = Object.freeze({ marketplaceRows, isMarketplaceManaged });
})(globalThis);

