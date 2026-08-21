import { describe, expect, it } from "vitest";

import { scaleAndClipBBox } from "./bbox";

describe("scaleAndClipBBox", () => {
  it("scales the horizontal and vertical axes independently", () => {
    expect(
      scaleAndClipBBox([100, 200, 500, 700], [1000, 1000], [500, 2000]),
    ).toEqual({
      left: 50,
      top: 400,
      width: 200,
      height: 1000,
    });
  });

  it("clips source coordinates before mapping them into the rendered page", () => {
    expect(
      scaleAndClipBBox([-10, 20, 120, 250], [100, 200], [200, 100]),
    ).toEqual({
      left: 0,
      top: 10,
      width: 200,
      height: 90,
    });
  });

  it("does not guess a coordinate system when the source page size is missing or invalid", () => {
    expect(scaleAndClipBBox([10, 20, 30, 40], null, [600, 800])).toBeNull();
    expect(scaleAndClipBBox([10, 20, 30, 40], [0, 1000], [600, 800])).toBeNull();
    expect(scaleAndClipBBox([10, 20, 30, 40], [1000, 1000], [0, 800])).toBeNull();
  });

  it("rejects malformed and zero-area boxes instead of drawing an out-of-bounds highlight", () => {
    expect(scaleAndClipBBox([50, 50, 20, 80], [100, 100], [200, 200])).toBeNull();
    expect(scaleAndClipBBox([20, 20, 20, 80], [100, 100], [200, 200])).toBeNull();
    expect(scaleAndClipBBox([Number.NaN, 10, 20, 30], [100, 100], [200, 200])).toBeNull();
  });
});
