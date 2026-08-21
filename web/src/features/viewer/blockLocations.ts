import type { DocumentBlock } from "../../api/types";

export type BlockLocation = {
  bbox: [number, number, number, number];
  pageIdx: number;
  pageSize: [number, number];
};

export function blockLocations(block: DocumentBlock): BlockLocation[] {
  const locations: BlockLocation[] = [];
  const bboxByPage = block.bbox_by_page;
  const pageSizeByPage = block.bbox_page_size_by_page;

  if (bboxByPage) {
    Object.entries(bboxByPage).forEach(([pageKey, boxes]) => {
      const pageIdx = Number(pageKey);
      const pageSize = pageSizeByPage?.[pageKey] ?? (
        pageIdx === block.page_idx ? block.page_size : null
      );
      if (!Number.isSafeInteger(pageIdx) || pageIdx < 0 || !pageSize) {
        return;
      }
      boxes.forEach((bbox) => locations.push({ bbox, pageIdx, pageSize }));
    });
  }

  if (locations.length === 0 && block.bbox && block.page_size) {
    locations.push({
      bbox: block.bbox,
      pageIdx: block.page_idx,
      pageSize: block.page_size,
    });
  }
  return locations.sort((left, right) => left.pageIdx - right.pageIdx);
}

export function blockAppearsOnPage(block: DocumentBlock, page: number) {
  return blockLocations(block).some((location) => location.pageIdx === page - 1);
}
