import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { readerRoute } from "../router";
import { fileUrl, Markdown, rawUrl, TypedText } from "./Artifacts";

type Item = components["schemas"]["LibraryItem"];

/** A file the library does not list (a run's note, a memory source), read as it is on disk. */
async function unlisted(worldId: string, path: string): Promise<Item | null> {
  const response = await globalThis.fetch(fileUrl(worldId, path)).catch(() => null);
  if (!response?.ok) return null;
  const name = path.split("/").pop() ?? path;
  const text = await response.text();
  return {
    path,
    name,
    type: path.includes("/notes/") ? "note" : "file",
    title: name,
    author: "",
    created: "",
    pinned: false,
    body: name.endsWith(".md") ? text.replace(/^---\n[\s\S]*?\n---\n/, "") : text,
    fields: {},
    run_id: /^runs\/([^/]+)\//.exec(path)?.[1] ?? "",
    run_name: "",
    workflow_id: "",
    workflow_version: null,
    stage_index: null,
    stage_name: "",
  };
}

/** A document artifact read in the app, wrapped to the screen: what it is, where it came from,
 * its typed text, then the document. Built from the library listing and the files route. */
export function Reader() {
  const { worldId } = readerRoute.useParams();
  const { path } = readerRoute.useSearch();
  const [doc, setDoc] = useState<Item | null | undefined>(undefined);

  useEffect(() => {
    let live = true;
    setDoc(undefined);
    api
      .GET("/api/worlds/{world_id}/library", { params: { path: { world_id: worldId } } })
      .then(({ data }) => data?.find((i) => i.path === path) ?? unlisted(worldId, path))
      .catch(() => unlisted(worldId, path))
      .then((d) => live && setDoc(d));
    return () => {
      live = false;
    };
  }, [worldId, path]);

  if (doc === undefined) {
    return (
      <main>
        <p role="status">Loading the document...</p>
      </main>
    );
  }
  if (!doc) {
    return (
      <main>
        <p role="alert">There is no document at {path || "this address"}.</p>
        <Link to="/worlds/$worldId/library" params={{ worldId }}>
          Back to the library
        </Link>
      </main>
    );
  }
  // runs/<run>/<stage>/...; a gate decision's folder is "02-pick", not a stage id.
  const stage = doc.path.split("/")[2] ?? "";
  return (
    <main className="reader">
      <header className="page">
        <div>
          <h1>{doc.title}</h1>
          <p className="sub">
            <span className="pill">{doc.type.replace(/_/g, " ")}</span>
          </p>
        </div>
      </header>
      <dl className="kv">
        {doc.run_id && (
          <>
            <dt>From</dt>
            <dd>
              <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: doc.run_id }}>
                {doc.run_name || "the run"}
              </Link>
              {doc.stage_index !== null && (
                <>
                  {" · "}
                  <Link
                    to="/worlds/$worldId/runs/$runId"
                    params={{ worldId, runId: doc.run_id }}
                    search={/^\d{2}-/.test(stage) ? {} : { stage }}
                  >
                    step {doc.stage_index + 1}, {doc.stage_name}
                  </Link>
                </>
              )}
            </dd>
          </>
        )}
        <dt>File</dt>
        <dd>
          <span className="mono">{doc.name}</span> ·{" "}
          <a href={rawUrl(worldId, doc)} target="_blank" rel="noreferrer">
            Raw file
          </a>
        </dd>
      </dl>
      <article className="panel" data-testid="document">
        {doc.fields.deleted ? (
          <p className="faint">{doc.name} was deleted by retention; its record stays.</p>
        ) : (
          <>
            <TypedText body={doc.body} />
            <Markdown text={doc.body} base={{ worldId, path: doc.path }} />
          </>
        )}
      </article>
    </main>
  );
}
