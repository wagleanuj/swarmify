import { type CSSProperties, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { plainRole } from "../skin";
import { POSE_WORDS, type Pose } from "./scene";

type Pack = components["schemas"]["SpritePack"];
type Character = components["schemas"]["Character"];
export type Member = components["schemas"]["CastMember"];

/** The pack shipped with Swarmify, which a world draws with until it picks its own.
 * ponytail: every world uses it until the skin setting gains `pack` (Crew, #287). */
const BUNDLED_PACK = "kenney-toon";

const frameUrl = (pack: string, character: string, frame: string) =>
  `/api/sprite-packs/${[pack, character, frame].map(encodeURIComponent).join("/")}`;

/** How one agent looks: their character in the pack, their shown name and plain label. */
export type Look = { pack: string; id: string; character: Character; name: string; label: string; colour?: string | null };

/** The world's cast and the pack it is drawn from, read again as the agent count changes. */
export function useCast(worldId: string, agentCount?: number) {
  const [cast, setCast] = useState<Member[]>([]);
  const [pack, setPack] = useState<Pack | null>(null);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/cast", { params: { path: { world_id: worldId } } })
      .then(({ data }) => data && setCast(data))
      .catch(() => undefined);
  }, [worldId, agentCount]);

  useEffect(() => {
    api
      .GET("/api/sprite-packs")
      .then(({ data }) => setPack(data?.find((p) => p.id === BUNDLED_PACK) ?? null))
      .catch(() => undefined);
  }, []);

  /** How the agent named `name` looks: the pack's casting by agent name, else by role, else a
   * default character picked by their place in the cast. A name not in the cast (a connector
   * that made an artifact, an agent the cast read has not caught up with) has no role of its
   * own. Only characters with an idle frame are drawn. */
  function look(name: string): Look | null {
    if (!pack) return null;
    const at = cast.findIndex((m) => m.name === name);
    const index = Math.max(0, at);
    const role = cast[at]?.role ?? name;
    const casting = pack.casting?.[name] ?? pack.casting?.[role];
    const drawable = Object.entries(pack.characters ?? {}).filter(([, c]) => c.poses.idle?.length);
    const [id, character] = drawable.find(([cid]) => cid === casting?.character) ?? drawable[index % drawable.length] ?? [];
    if (!id || !character) return null;
    const label = plainRole(name);
    return { pack: pack.id, id, character, name: casting?.name ?? label, label, colour: casting?.colour };
  }

  /** The name the office calls an agent by: their casting's name, else the plain role label. */
  const nameOf = (name: string) => look(name)?.name ?? plainRole(name);

  return { cast, look, nameOf };
}

export const still = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** A frame counter for the cosmetic motion; it stands at 0 with reduced motion. */
function useTick(moving: boolean) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!moving) return;
    const timer = setInterval(() => setTick((n) => n + 1), 150);
    return () => clearInterval(timer);
  }, [moving]);
  return tick;
}

/** The pose drawn at `tick`, from the pose the data says: the planner alternates thinking and
 * working; a roaming worker walks to the whiteboard, draws there, and walks back. Cosmetic. */
function drawn(pose: Pose, tick: number, roam: boolean): { pose: Pose; away: boolean } {
  if (pose === "think") return { pose: Math.floor(tick / 16) % 2 ? "work" : "think", away: false };
  if (!roam || pose !== "work") return { pose, away: false };
  const phase = tick % 64;
  if (phase < 40) return { pose: "work", away: false };
  return { pose: phase < 47 || phase >= 57 ? "walk" : "back", away: true };
}

/** One character in a pose, with an optional speech bubble. It walks in when it first appears
 * (an agent arriving at a new place) and animates its pose, both only when motion is allowed. */
export function Actor({ look, pose, say, roam = false }: { look: Look; pose: Pose; say?: string | null; roam?: boolean }) {
  const [moving] = useState(() => !still());
  const [arriving, setArriving] = useState(moving);
  const tick = useTick(moving);
  useEffect(() => {
    const timer = setTimeout(() => setArriving(false), 1200);
    return () => clearTimeout(timer);
  }, []);

  const now = arriving ? { pose: "walk" as const, away: false } : drawn(pose, tick, roam);
  const poses = look.character.poses;
  const frames = poses[now.pose]?.length ? poses[now.pose] : poses.idle;
  const frame = frames?.[tick % frames.length] ?? "";
  const words = POSE_WORDS[arriving ? "walk" : pose];
  const alt = [look.name, look.name === look.label ? "" : look.label, words].filter(Boolean).join(", ");
  return (
    <li className={`office-actor${now.away ? " office-away" : ""}`} style={{ "--accent": look.colour ?? undefined } as CSSProperties}>
      <img src={frameUrl(look.pack, look.id, frame)} alt={alt} />
      <span className="office-tag" aria-hidden="true">
        {look.name}
      </span>
      {say && <q className={`office-bubble${say === "!" ? " office-alert" : ""}`}>{say}</q>}
    </li>
  );
}

/** The cast members off the floor, standing in the office: idle, or cheering or stuck after
 * their latest run. */
export function Bench({ members }: { members: { name: string; look: Look; pose: Pose }[] }) {
  return (
    <ul className="office-cast" aria-label="The cast">
      {members.map(({ name, look, pose }) => (
        <Actor key={name} look={look} pose={pose} />
      ))}
    </ul>
  );
}
