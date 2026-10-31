import { Link } from "@tanstack/react-router";
import { type CSSProperties, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { BUNDLED_PACK, plainRole, type Skin } from "../skin";
import { POSE_WORDS, type Pose } from "./scene";

export type Pack = components["schemas"]["SpritePack"];
export type Member = components["schemas"]["CastMember"];
export type Casting = components["schemas"]["Casting"];
type Character = components["schemas"]["Character"];

const frameUrl = (pack: string, character: string, frame: string) =>
  `/api/sprite-packs/${[pack, character, frame].map(encodeURIComponent).join("/")}`;

/** The first frame of a pose, or of idle when the character lacks that pose. */
export function poseSrc(pack: Pack, id: string, character: Character, pose: string) {
  const frames = character.poses[pose]?.length ? character.poses[pose] : (character.poses.idle ?? []);
  return frameUrl(pack.id, id, frames[0] ?? "");
}

type Named = Pick<Member, "name" | "role">;

/** The pack's casting for an agent: by agent name, else by role. */
export const packCasting = (pack: Pack, member: Named): Casting => pack.casting?.[member.name] ?? pack.casting?.[member.role] ?? {};

/** How an agent looks: the world's own casting field by field, over the pack's casting, else a
 * default character picked by their place in the cast and the plain name. Only characters with
 * an idle frame are drawn; a wanted character the pack lacks is `missing`. */
export function look(pack: Pack, member: Named, index: number, own: Casting = {}) {
  const base = packCasting(pack, member);
  const field = (k: keyof Casting) => own[k] ?? base[k] ?? null;
  const drawable = Object.entries(pack.characters ?? {}).filter(([, c]) => c.poses.idle?.length);
  const wanted = field("character");
  const [id, character] =
    drawable.find(([cid]) => cid === wanted) ?? drawable.find(([cid]) => cid === base.character) ?? drawable[index % drawable.length] ?? [];
  if (!id || !character) return null;
  return {
    id,
    character,
    src: poseSrc(pack, id, character, "idle"),
    name: field("name") ?? plainRole(member.name),
    colour: field("colour"),
    catchphrase: field("catchphrase"),
    missing: wanted && wanted !== id ? wanted : null,
  };
}

/** The world's pack, else the bundled one when the world's is no longer installed. */
export const packOf = (packs: Pack[], skin?: Skin) =>
  packs.find((p) => p.id === (skin?.pack ?? BUNDLED_PACK)) ?? packs.find((p) => p.id === BUNDLED_PACK) ?? null;

/** How one agent looks in the office: their character in the pack, their shown name, plain
 * agent name and label. */
export type Look = { pack: string; agent: string; id: string; character: Character; name: string; label: string; colour?: string | null };

/** The world's cast and sprite packs, read when the office opens; the cast again when the
 * world's agent count changes, as it grows and shrinks with it. */
export function useCrew(worldId: string, agentCount?: number) {
  const [cast, setCast] = useState<Member[]>([]);
  const [packs, setPacks] = useState<Pack[]>([]);
  // Whether the pack list has answered (or failed), so a room is not drawn in the wrong style.
  const [read, setRead] = useState(false);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/cast", { params: { path: { world_id: worldId } } })
      .then(({ data }) => data && setCast(data))
      .catch(() => undefined);
  }, [worldId, agentCount]);

  useEffect(() => {
    api
      .GET("/api/sprite-packs")
      .then(({ data }) => data && setPacks(data))
      .catch(() => undefined)
      .finally(() => setRead(true));
  }, []);

  return { cast, packs, read };
}

/** The world's cast drawn from its skin: its pack, with each agent's own casting over the pack's. */
export function useCast(worldId: string, agentCount?: number, skin?: Skin) {
  const { cast, packs, read } = useCrew(worldId, agentCount);
  const pack = packOf(packs, skin);

  /** How the agent named `name` looks: the pack's casting by agent name, else by role, else a
   * default character picked by their place in the cast. A name not in the cast (a connector
   * that made an artifact, an agent the cast read has not caught up with) has no role of its
   * own. Only characters with an idle frame are drawn. */
  function lookOf(name: string): Look | null {
    if (!pack) return null;
    const at = cast.findIndex((m) => m.name === name);
    const seen = look(pack, cast[at] ?? { name, role: name }, Math.max(0, at), skin?.cast?.[name]);
    if (!seen) return null;
    return { pack: pack.id, agent: name, id: seen.id, character: seen.character, name: seen.name, label: plainRole(name), colour: seen.colour };
  }

  /** The name the office calls an agent by: their casting's name, else the plain role label. */
  const nameOf = (name: string) => lookOf(name)?.name ?? plainRole(name);

  /** The world's pack, null when there is none, undefined until the pack list answers. */
  return { cast, look: lookOf, nameOf, pack: read ? pack : undefined };
}

/** The first frame of `pose` (idle when the character lacks it): someone standing still in it. */
export function stillFrame(look: Look, pose: Pose) {
  const poses = look.character.poses;
  return frameUrl(look.pack, look.id, (poses[pose]?.length ? poses[pose] : poses.idle)?.[0] ?? "");
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
export function Actor({ look, pose, say, roam = false, crewOf }: { look: Look; pose: Pose; say?: string | null; roam?: boolean; crewOf?: string }) {
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
      {crewOf ? (
        <Link to="/worlds/$worldId/office/crew" params={{ worldId: crewOf }} search={{ agent: look.agent }}>
          <img src={frameUrl(look.pack, look.id, frame)} alt={alt} />
        </Link>
      ) : (
        <img src={frameUrl(look.pack, look.id, frame)} alt={alt} />
      )}
      <span className="office-tag" aria-hidden="true">
        {look.name}
      </span>
      {say && <q className={`office-bubble${say === "!" ? " office-alert" : ""}`}>{say}</q>}
    </li>
  );
}

/** The cast members off the floor, standing in the office: idle, or cheering or stuck after
 * their latest run. Each opens in Crew. */
export function Bench({ worldId, members }: { worldId: string; members: { name: string; look: Look; pose: Pose }[] }) {
  return (
    <ul className="office-cast" aria-label="The cast">
      {members.map(({ name, look, pose }) => (
        <Actor key={name} look={look} pose={pose} crewOf={worldId} />
      ))}
    </ul>
  );
}
