export type BBox = readonly [number, number, number, number];
export type PageSize = readonly [number, number];

export type PositionedBBox = {
  left: number;
  top: number;
  width: number;
  height: number;
};

function validSize(size: PageSize | null | undefined): size is PageSize {
  return Boolean(
    size &&
      size.length === 2 &&
      size.every((value) => Number.isFinite(value) && value > 0),
  );
}

export function scaleAndClipBBox(
  bbox: BBox | null | undefined,
  sourcePageSize: PageSize | null | undefined,
  renderedPageSize: PageSize | null | undefined,
): PositionedBBox | null {
  if (
    !bbox ||
    bbox.length !== 4 ||
    !bbox.every(Number.isFinite) ||
    !validSize(sourcePageSize) ||
    !validSize(renderedPageSize)
  ) {
    return null;
  }

  const [sourceWidth, sourceHeight] = sourcePageSize;
  const [renderedWidth, renderedHeight] = renderedPageSize;
  const [rawLeft, rawTop, rawRight, rawBottom] = bbox;
  if (rawRight <= rawLeft || rawBottom <= rawTop) {
    return null;
  }

  const left = Math.min(sourceWidth, Math.max(0, rawLeft));
  const top = Math.min(sourceHeight, Math.max(0, rawTop));
  const right = Math.min(sourceWidth, Math.max(0, rawRight));
  const bottom = Math.min(sourceHeight, Math.max(0, rawBottom));
  if (right <= left || bottom <= top) {
    return null;
  }

  const scaleX = renderedWidth / sourceWidth;
  const scaleY = renderedHeight / sourceHeight;
  return {
    left: left * scaleX,
    top: top * scaleY,
    width: (right - left) * scaleX,
    height: (bottom - top) * scaleY,
  };
}
