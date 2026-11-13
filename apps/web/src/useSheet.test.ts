import { expect, test } from "vitest";

import { CLOSE_AT, closes, FLICK, follow } from "./useSheet";

test("a released sheet closes past its threshold or on a downward flick, and springs back otherwise", () => {
  const h = 400;
  expect(closes(h * CLOSE_AT + 1, h, 0)).toBe(true); // dragged far enough, let go slowly
  expect(closes(h * CLOSE_AT - 1, h, 0)).toBe(false); // short and slow: springs back
  expect(closes(30, h, FLICK + 0.1)).toBe(true); // short but flicked down
  expect(closes(30, h, FLICK - 0.1)).toBe(false);
  expect(closes(-30, h, 2)).toBe(false); // flicked up, never closes
  expect(closes(0, h, 2)).toBe(false);
});

test("the sheet follows a finger 1:1 down and with growing resistance up", () => {
  expect(follow(0)).toBe(0);
  expect(follow(120)).toBe(120);
  expect(follow(-20)).toBeLessThan(0);
  expect(follow(-20)).toBeGreaterThan(-20);
  expect(follow(-200) - follow(-100)).toBeGreaterThan(-100 / 4); // the second 100px gives far less
});
