import { type CSSProperties, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { plainRole } from "../skin";

type Pack = components["schemas"]["SpritePack"];
type Member = components["schemas"]["CastMember"];

/** The pack shipped with Swarmify, which a world draws with until it picks its own.
 * ponytail: every world uses it until the skin setting gains `pack` (Crew, #287). */
const BUNDLED_PACK = "kenney-toon";

const frameUrl = (pack: string, character: string, frame: string) =>
  `/api/sprite-packs/${[pack, character, frame].map(encodeURIComponent).join("/")}`;

/** How an agent looks: the pack's casting by agent name, else by role, else a default character
 * picked by their place in the cast. Only characters with an idle frame are drawn. */
function look(pack: Pack, member: Member, index: number) {
  const casting = pack.casting?.[member.name] ?? pack.casting?.[member.role];
  const drawable = Object.entries(pack.characters ?? {}).filter(([, c]) => c.poses.idle?.length);
  const [id, character] =
    drawable.find(([cid]) => cid === casting?.character) ?? drawable[index % drawable.length] ?? [];
  if (!id || !character) return null;
  // ponytail: idle only; other poses (falling back to idle when a character lacks one) come with #285.
  const [idle = ""] = character.poses.idle ?? [];
  return { src: frameUrl(pack.id, id, idle), name: casting?.name ?? plainRole(member.name), colour: casting?.colour };
}

/** The world's cast standing idle in the office, each drawn from the world's sprite pack. */
export function StandingCast({ worldId, agentCount }: { worldId: string; agentCount?: number }) {
  const [cast, setCast] = useState<Member[]>([]);
  const [pack, setPack] = useState<Pack | null>(null);

  // The cast grows and shrinks with the world's agent count, so it is read again when that changes.
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

  if (!pack) return null;
  return (
    <ul className="office-cast" aria-label="The cast">
      {cast.map((member, i) => {
        const seen = look(pack, member, i);
        if (!seen) return null;
        const label = plainRole(member.name);
        return (
          <li key={member.name} className="office-actor" style={{ "--accent": seen.colour ?? undefined } as CSSProperties}>
            <img src={seen.src} alt={seen.name === label ? label : `${seen.name}, ${label}`} />
            <span className="office-tag" aria-hidden="true">
              {seen.name}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
