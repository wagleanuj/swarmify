import "@fontsource/pixelify-sans/500.css";
import "@fontsource/pixelify-sans/700.css";
import { createContext, type CSSProperties, type ReactNode, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { Pack } from "./cast";

/** The pixel room a pixel pack is drawn in: flat-on, laid out in art pixels and scaled up by one
 * whole number, so characters, props and text share one pixel size. Everything the room needs
 * is drawn here in code; a pack may draw any of its props itself. */

const INK = "#2b2840";
type Rect = (x: number, y: number, w: number, h: number, c: string) => void;
type Sprite = [w: number, h: number, draw: (R: Rect, box: (x: number, y: number, w: number, h: number, fill: string) => void) => void];

const table = (top: string, hi: string, front: string): Sprite => [
  96,
  22,
  (R, box) => {
    box(0, 0, 96, 8, top);
    R(1, 1, 94, 1, hi);
    box(0, 7, 96, 9, front);
    box(4, 15, 5, 7, front);
    box(87, 15, 5, 7, front);
  },
];

const SPRITES: Record<string, Sprite> = {
  table_on: table("#7a5232", "#9a6a42", "#5e3d24"),
  table_off: table("#c79d6c", "#e0b27c", "#a87b4c"),
  // The back of an open lid, the screen's glow over its top edge.
  laptop_on: [
    20,
    15,
    (R, box) => (R(1, 0, 18, 1, "#9be8ff"), box(0, 1, 20, 13, "#c9cfdb"), R(1, 2, 18, 1, "#e6eaf2"), R(9, 6, 2, 2, "#fff"), R(2, 14, 16, 1, "#8b88a3")),
  ],
  laptop_off: [20, 5, (R, box) => (box(0, 0, 20, 4, "#a7adbb"), R(1, 1, 18, 1, "#c9cfdb"), R(1, 4, 18, 1, "#8b88a3"))],
  cup: [6, 7, (R, box) => (box(0, 0, 6, 7, "#fff8d6"), R(1, 1, 4, 1, "#6b3e1f"))],
  cooler: [16, 40, (R, box) => (box(3, 0, 10, 14, "#9fd3f5"), R(5, 2, 2, 10, "#d6f0ff"), box(1, 13, 14, 27, "#e9edf5"), R(6, 20, 4, 2, "#5aa9ff"))],
  fridge: [24, 52, (R, box) => (box(0, 0, 24, 52, "#e9edf5"), R(1, 18, 22, 1, INK), R(19, 6, 2, 8, "#8b88a3"), R(19, 22, 2, 12, "#8b88a3"))],
  kitchen: [
    56,
    30,
    (R, box) => {
      box(0, 4, 56, 26, "#d79a66");
      box(0, 0, 56, 5, "#d9d4c7");
      R(22, 1, 14, 2, "#9aa3b8");
      R(18, 6, 1, 23, INK);
      R(37, 6, 1, 23, INK);
      for (const x of [14, 21, 33, 40]) R(x, 14, 2, 2, INK);
    },
  ],
  bookshelf: [
    30,
    40,
    (R, box) => {
      box(0, 0, 30, 40, "#8d5d39");
      ["#e8546b", "#5aa9ff", "#ffc93c", "#3fbf7f", "#9a7bff", "#ff8a3d"].forEach((c, i) => {
        R(3 + i * 4, 3, 3, 9, c);
        R(27 - i * 4, 16, 3, 9, c);
        R(5 + i * 3, 30, 2, 8, c);
      });
      R(1, 12, 28, 2, "#6b4428");
      R(1, 25, 28, 2, "#6b4428");
    },
  ],
  clock: [12, 12, (R, box) => (box(0, 0, 12, 12, "#fff"), R(5, 3, 1, 4, INK), R(6, 6, 3, 1, INK))],
  cart: [22, 18, (R, box) => (box(2, 0, 16, 11, "#c79d6c"), R(9, 1, 2, 9, "#a87b4c"), box(0, 10, 22, 4, "#8b88a3"), R(2, 14, 4, 4, INK), R(16, 14, 4, 4, INK))],
  plant: [
    20,
    40,
    (R, box) => {
      for (const [x, y, w, h] of [
        [7, 0, 6, 14],
        [1, 6, 6, 12],
        [13, 5, 6, 13],
        [4, 14, 12, 12],
      ] as const)
        box(x, y, w, h, "#3a8f4a");
      R(8, 1, 1, 12, "#58b85f");
      R(2, 7, 1, 10, "#58b85f");
      box(4, 26, 12, 14, "#c4603f");
      R(3, 26, 14, 3, "#d97a55");
    },
  ],
  sofa: [
    64,
    26,
    (R, box) => {
      box(2, 0, 60, 14, "#4a6fb5");
      box(4, 10, 56, 12, "#5b80c6");
      R(6, 12, 25, 1, "#86a6e6");
      R(33, 12, 25, 1, "#86a6e6");
      R(32, 10, 1, 10, INK);
      box(0, 4, 7, 18, "#3d5fa3");
      box(57, 4, 7, 18, "#3d5fa3");
      R(3, 22, 3, 4, INK);
      R(58, 22, 3, 4, INK);
    },
  ],
  // Your office: a glass room with a door, in the front corner.
  office: [
    92,
    62,
    (R, box) => {
      box(0, 0, 92, 62, "#9a97b5");
      R(3, 3, 86, 59, "#cfe9f7");
      for (let k = 0; k < 3; k++) R(10 + k * 4, 6 + k, 1, 16, "#ffffffaa");
      box(64, 22, 22, 40, "#8d5d39");
      R(81, 40, 2, 2, "#ffc93c");
    },
  ],
};

const drawn = new Map<string, string>();
/** A built-in prop as an image, drawn once at one art pixel per pixel. */
function builtIn(name: string): string {
  const sprite = SPRITES[name];
  if (!sprite) return "";
  if (!drawn.has(name)) {
    const [w, h, draw] = sprite;
    const canvas = Object.assign(document.createElement("canvas"), {
      width: w,
      height: h,
    });
    const g = canvas.getContext("2d");
    const R: Rect = (x, y, rw, rh, c) => {
      if (!g) return;
      g.fillStyle = c;
      g.fillRect(x, y, rw, rh);
    };
    draw(R, (x, y, bw, bh, fill) => (R(x, y, bw, bh, INK), R(x + 1, y + 1, bw - 2, bh - 2, fill)));
    drawn.set(name, canvas.toDataURL());
  }
  return drawn.get(name) ?? "";
}

const packUrl = (pack: Pack, file: string) => `/api/sprite-packs/${[pack.id, ...file.split("/")].map(encodeURIComponent).join("/")}`;

const PixelPack = createContext<Pack | null>(null);
/** The pixel pack the room is drawn for, or null in the cartoon room. */
export const usePixel = () => useContext(PixelPack);

/** A room prop at its art size: the pack's own drawing when it has one, else the built-in one. */
export function Prop({ name, className = "" }: { name: string; className?: string }) {
  const pack = usePixel();
  const own = pack?.room?.props?.[name];
  return <img className={`office-prop office-prop-${name} ${className}`} src={pack && own ? packUrl(pack, own) : builtIn(name)} alt="" />;
}

/** A pod's table with a laptop for each seat (at least one), open and glowing while the crew
 * works, shut otherwise, and a cup after the first. */
export function Table({ on, seats }: { on: boolean; seats: number }) {
  const laptop = on ? "laptop_on" : "laptop_off";
  return (
    <div className="office-table" aria-hidden="true">
      <Prop name={on ? "table_on" : "table_off"} />
      <span className="office-laptops">
        <Prop name={laptop} />
        <Prop name="cup" />
        {Array.from({ length: seats - 1 }, (_, i) => (
          <Prop key={i} name={laptop} />
        ))}
      </span>
    </div>
  );
}

/** One art pixel in screen pixels: a whole number, the largest that keeps at least a 430 by 400
 * art pixel room on screen, the size the room's pieces are laid out for. */
const scaleFor = (w: number, h: number) => Math.max(1, Math.floor(Math.min(w / 430, h / 400)));

const WALL = 84;

/** The room's layer in art pixels, scaled by one whole number to fill the room: the floor, the
 * wall with its windows, door, kitchen, bookshelf and clock, the plants and the world's sign, all
 * under the pods, Your office and the cast it holds. */
export function PixelRoom({ pack, name, children }: { pack: Pack; name: string; children: ReactNode }) {
  const outer = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  useLayoutEffect(() => {
    const el = outer.current;
    if (!el) return;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const watch = new ResizeObserver(measure);
    watch.observe(el);
    return () => watch.disconnect();
  }, []);
  const k = size ? scaleFor(size.w, size.h) : 1;
  const art = size && { w: Math.floor(size.w / k), h: Math.floor(size.h / k) };
  const [fw, fh] = pack.frame ?? [64, 64];
  const tile = pack.room?.props?.floor_tile;
  const style = {
    width: art?.w,
    height: art?.h,
    transform: `scale(${k})`,
    "--frame-w": `${fw}px`,
    "--frame-h": `${fh}px`,
    backgroundImage: tile ? `url("${packUrl(pack, tile)}")` : undefined,
  } as CSSProperties;
  return (
    <div ref={outer} className="office-pixel-fit">
      {art && (
        <PixelPack.Provider value={pack}>
          <div className="office-pixel-art" style={style}>
            <Backdrop w={art.w} h={art.h} floor={!tile} />
            <div className="office-pixel-wall" aria-hidden="true">
              <Prop name="clock" className="office-at-clock" />
              <Prop name="bookshelf" className="office-at-books" />
              <Prop name="cooler" className="office-at-cooler" />
              <span className="office-at-kitchen">
                <Prop name="kitchen" />
                <Prop name="fridge" />
              </span>
              <Prop name="plant" className="office-at-plant-l" />
              <Prop name="plant" className="office-at-plant-r" />
              <div className="office-neon">{name}</div>
            </div>
            {children}
          </div>
        </PixelPack.Provider>
      )}
    </div>
  );
}

/** The floor's oak planks (unless the pack tiles its own floor) and the back wall, with its
 * panels, baseboard, windows onto a skyline and the door, on one canvas at one pixel per art
 * pixel. */
function Backdrop({ w, h, floor }: { w: number; h: number; floor: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const g = ref.current?.getContext("2d");
    if (!g) return;
    const R: Rect = (x, y, rw, rh, c) => {
      g.fillStyle = c;
      g.fillRect(Math.round(x), Math.round(y), Math.round(rw), Math.round(rh));
    };
    const box = (x: number, y: number, bw: number, bh: number, fill: string) => (R(x, y, bw, bh, INK), R(x + 1, y + 1, bw - 2, bh - 2, fill));
    g.clearRect(0, 0, w, h);
    if (floor) {
      R(0, WALL, w, h - WALL, "#d6a46c");
      for (let y = WALL, row = 0; y < h; y += 6, row++) {
        R(0, y + 5, w, 1, "#b98552");
        for (let x = (row * 23) % 41; x < w; x += 41) R(x, y, 1, 5, "#b98552");
        R(0, y, w, 1, "#e0b27c");
      }
    }
    g.globalAlpha = 0.18;
    R(0, WALL, w, 4, "#000");
    g.globalAlpha = 1;
    R(0, 0, w, WALL, "#efe2c4");
    for (let x = 12; x < w; x += 24) R(x, 6, 1, WALL - 14, "#e0cfa8");
    R(0, 0, w, 5, "#b98a57");
    R(0, 5, w, 1, INK);
    R(0, WALL - 8, w, 8, "#7a5232");
    R(0, WALL - 8, w, 1, "#9a6a42");
    R(0, WALL - 1, w, 1, INK);
    const n = w < 300 ? 3 : 5;
    const x0 = w * 0.22;
    const ww = (w * 0.56 - 8 * (n - 1)) / n;
    for (let i = 0; i < n; i++) {
      const x = x0 + i * (ww + 8);
      box(x, 10, ww, 30, "#c9cfdb");
      R(x + 2, 12, ww - 4, 13, "#a9dcf7");
      R(x + 2, 25, ww - 4, 13, "#d8f0fc");
      for (let b = 0; b < ww - 6; b += 5) {
        const bh = 3 + ((b * 7 + i * 3) % 7);
        R(x + 3 + b, 38 - bh, 4, bh, "#8fa3c8");
      }
      R(x - 1, 40, ww + 2, 2, "#fff");
    }
    box(8, WALL - 58, 30, 50, "#6d6a86");
    R(11, WALL - 52, 11, 44, "#bfe6ff");
    R(24, WALL - 52, 11, 44, "#bfe6ff");
  }, [w, h, floor]);
  return <canvas ref={ref} className="office-backdrop" width={w} height={h} aria-hidden="true" />;
}
