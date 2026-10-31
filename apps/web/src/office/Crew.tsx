import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { type CSSProperties, type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import { useWorldEvents } from "../api/events";
import type { components } from "../api/schema";
import { BUNDLED_PACK, plainRole, saveSkin, type Skin } from "../skin";
import { type Casting, look, type Member, type Pack, packCasting, packOf, poseSrc, useCrew } from "./cast";
import "./office.css";

type World = components["schemas"]["World"];

/** Every pose a character can strike, as Crew previews them. */
const POSES: [string, string][] = [
  ["idle", "Idle"],
  ["think", "Thinking"],
  ["work", "Working"],
  ["walk", "Walking"],
  ["talk", "Asking"],
  ["cheer", "Shipped"],
  ["stuck", "Stuck"],
  ["hold", "Carrying"],
  ["back", "At the whiteboard"],
];
const LOOK_FIELDS = ["character", "name", "colour", "catchphrase"] as const;

/** Only the fields an agent's own casting sets, trimmed; empty text is no change. */
function own(casting: Casting = {}): Casting {
  return Object.fromEntries(LOOK_FIELDS.flatMap((k) => (casting[k]?.trim() ? [[k, casting[k].trim()]] : [])));
}

/** Where the operator dresses the cast: pick each agent's character from the world's sprite
 * pack, name them, give them a colour and a catchphrase, and preview every pose. All of it is
 * the world's skin setting; code and logs keep the plain agent names (ADR 0004). */
export function Crew() {
  const { worldId } = useParams({ from: "/worlds/$worldId/office/crew" });
  const { agent } = useSearch({ from: "/worlds/$worldId/office/crew" });
  const navigate = useNavigate();
  const [world, setWorld] = useState<World | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const { cast, packs } = useCrew(worldId, world?.agent_count);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data, response }) => {
        if (response.status === 404) return setWorld(null);
        if (!data) throw new Error("load failed");
        setWorld(data);
      })
      .catch(() => setError("Could not load this world. Is the API running?"));
  }, [worldId]);

  useWorldEvents(worldId, {
    "world.updated": (changes) => setWorld((prev) => prev && { ...prev, ...changes }),
  });

  if (world === null) {
    return (
      <div className="office-app">
        <p className="office-note" role="alert">
          No such world. <Link to="/">Back to your worlds.</Link>
        </p>
      </div>
    );
  }

  const skin: Skin = world?.skin ?? { view: "office", pack: BUNDLED_PACK };
  const pack = packOf(packs, world?.skin);
  const index = Math.max(0, cast.findIndex((m) => m.name === agent));
  const member = cast[index];

  /** One world settings write of the whole skin, keeping only agents still in the cast. */
  async function save(next: Skin) {
    setSaved(false);
    const names = new Set(cast.map((m) => m.name));
    const looks = Object.entries(next.cast ?? {}).flatMap(([n, c]) => (names.has(n) && Object.keys(own(c)).length ? [[n, own(c)]] : []));
    const changed = await saveSkin(worldId, { ...next, cast: Object.fromEntries(looks) });
    if (!changed) return setError("Could not save. Check the name is at most 24 characters and the catchphrase 80.");
    setError(null);
    setWorld(changed);
    setSaved(true);
  }

  return (
    <div className="office-app">
      <header className="office-bar" aria-label="Crew bar">
        <div className="office-chip office-world">
          <small>Crew of</small>
          <h1>{world?.name ?? "..."}</h1>
        </div>
        <Link className="office-btn" to="/worlds/$worldId/office" params={{ worldId }}>
          Back to the office
        </Link>
      </header>
      {error && (
        <p className="office-note" role="alert">
          {error}
        </p>
      )}
      <div className="office-dressing">
        <aside className="office-roster" aria-label="Crew">
          <h2>Crew</h2>
          <ul>
            {pack &&
              cast.map((m, i) => {
                const seen = look(pack, m, i, skin.cast?.[m.name]);
                return (
                  <li key={m.name}>
                    <button
                      type="button"
                      aria-pressed={m === member}
                      onClick={() => {
                        setSaved(false);
                        void navigate({ to: "/worlds/$worldId/office/crew", params: { worldId }, search: { agent: m.name } });
                      }}
                    >
                      {seen && <img src={seen.src} alt="" />}
                      <b>{seen?.name ?? plainRole(m.name)}</b>
                      <small>{plainRole(m.name)}</small>
                    </button>
                  </li>
                );
              })}
          </ul>
          <p>Names and looks are the skin's only. Code and logs keep the plain names.</p>
        </aside>
        {pack && member && (
          <Dresser
            // A save or another tab's change starts the form again from what the world holds.
            key={`${member.name} ${pack.id} ${JSON.stringify(own(skin.cast?.[member.name]))}`}
            member={member}
            index={index}
            pack={pack}
            packs={packs}
            skin={skin}
            saved={saved}
            save={save}
          />
        )}
      </div>
    </div>
  );
}

type DresserProps = {
  member: Member;
  index: number;
  pack: Pack;
  packs: Pack[];
  skin: Skin;
  saved: boolean;
  save: (next: Skin) => Promise<void>;
};

/** The selected agent on a pedestal in a chosen pose, and the form that dresses them. */
function Dresser({ member, index, pack, packs, skin, saved, save }: DresserProps) {
  const [draft, setDraft] = useState<Casting>(own(skin.cast?.[member.name]));
  const [pose, setPose] = useState("idle");
  const seen = look(pack, member, index, draft);
  if (!seen) return <p className="office-note">{pack.name} has no character to draw.</p>;
  const base = packCasting(pack, member);
  const label = plainRole(member.name);
  const set = (k: keyof Casting, v: string) => setDraft((d) => ({ ...d, [k]: v }));
  const looks = { ...skin.cast };
  delete looks[member.name];

  function submit(e: FormEvent) {
    e.preventDefault();
    void save({ ...skin, cast: { ...skin.cast, [member.name]: draft } });
  }

  return (
    <>
      <section className="office-stage" aria-label="Stage" style={{ "--accent": seen.colour ?? undefined } as CSSProperties}>
        <h2>{seen.name}</h2>
        <p className="office-role">{label}</p>
        {seen.catchphrase && <p className="office-catch">“{seen.catchphrase}”</p>}
        <img className="office-big" src={poseSrc(pack, seen.id, seen.character, pose)} alt={`${seen.name}, ${POSES.find(([p]) => p === pose)?.[1]}`} />
        <div className="office-poses" role="group" aria-label="Poses">
          {POSES.map(([p, l]) => (
            <button key={p} type="button" className="office-btn" aria-pressed={p === pose} onClick={() => setPose(p)}>
              {l}
            </button>
          ))}
        </div>
      </section>
      <form className="office-dress" aria-label={`Dress ${label}`} onSubmit={submit}>
        <h2>Dress {seen.name}</h2>
        <div role="group" aria-label="Sprite pack">
          <small>Sprite pack</small>
          {packs.map((p) => (
            <button key={p.id} type="button" className="office-btn" aria-pressed={p.id === pack.id} onClick={() => void save({ ...skin, pack: p.id, cast: { ...skin.cast, [member.name]: draft } })}>
              {p.name}
            </button>
          ))}
        </div>
        <p className="office-licence">
          Licence: {pack.licence || "none given"}
          {pack.author && <> · by {pack.author}</>}
        </p>
        {seen.missing && (
          <p className="office-note" role="status">
            {pack.name} has no character {seen.missing}, so {seen.name} wears {seen.character.name}. Pick another.
          </p>
        )}
        <div role="group" aria-label="Character" className="office-looks">
          {Object.entries(pack.characters ?? {})
            .filter(([, c]) => c.poses.idle?.length)
            .map(([id, c]) => (
              <button key={id} type="button" aria-pressed={id === seen.id} onClick={() => set("character", id)}>
                <img src={poseSrc(pack, id, c, "idle")} alt={c.name} />
              </button>
            ))}
        </div>
        <label>
          Name, up to 24 characters
          <input value={draft.name ?? ""} placeholder={base.name ?? label} maxLength={24} onChange={(e) => set("name", e.target.value)} />
        </label>
        <label>
          Colour
          <input type="color" value={seen.colour ?? "#ffc93c"} onChange={(e) => set("colour", e.target.value)} />
        </label>
        <label>
          Catchphrase, said when a run ships, up to 80 characters
          <input
            value={draft.catchphrase ?? ""}
            placeholder={base.catchphrase ?? ""}
            maxLength={80}
            onChange={(e) => set("catchphrase", e.target.value)}
          />
        </label>
        <div className="office-actions">
          <button type="submit" className="office-btn">
            Save {seen.name}
          </button>
          <button type="button" className="office-btn" onClick={() => void save({ ...skin, cast: looks })}>
            Reset to the pack's casting
          </button>
        </div>
        {saved && <p role="status">Saved. The office shows it now.</p>}
      </form>
    </>
  );
}
