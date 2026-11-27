"use client";

import { useState } from "react";

/**
 * Has the form been changed since the server state was loaded or saved? Used to grey out "Save" when there is nothing to save.
 *
 * `values` is everything the form can change (secrets and "clear" switches included: typing a new secret IS a change). `version` is the
 * server object the form was last filled from (the `cfg` that `apply()` stores): whenever it changes, the current values become the new
 * baseline, so a successful save (which applies the answer of the server) makes the form clean again, a failed save does not.
 * Until `version` exists (still loading) nothing is dirty.
 */
export function useDirty(values: unknown, version: unknown): boolean {
  const serialized = JSON.stringify(values);
  const [baseline, setBaseline] = useState<{ version: unknown; serialized: string }>({ version, serialized });
  // "Adjusting state while rendering" (React docs): the form fields and `version` were set in the same update, so `values` already
  // belong to the new server state when this runs.
  if (baseline.version !== version) setBaseline({ version, serialized });
  return version != null && baseline.serialized !== serialized;
}

/**
 * For a dialog that opens filled with the object being edited and closes when it is saved: has anything changed since it opened?
 * (`useDirty` is for sections that load from the server and apply the answer of a save.) `values` must hold EVERYTHING the dialog lets
 * the user change, otherwise "Save" would stay grey although something was changed.
 */
export function useChanged(values: unknown): boolean {
  const serialized = JSON.stringify(values);
  const [initial] = useState(serialized);
  return initial !== serialized;
}
