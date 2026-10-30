import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import { Wrapper } from "../test-utils";
import { INITIAL_STATE, toApiParams, type BuiltinPreset, type ForecastState, type PresetRef, type SavedPreset } from "./params";
import { PresetBar } from "./preset-bar";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, apiClient: { GET: vi.fn(), POST: vi.fn(), PUT: vi.fn(), DELETE: vi.fn() } };
});

const resolved = { baseYear: 2024, targetYear: 2034 };
const BUILTIN: BuiltinPreset[] = [
  { key: "moderate", name: "Moderate", description: "Moderate preset.", params: { ...toApiParams(INITIAL_STATE, resolved), horizon_years: null } },
];
const MINE: SavedPreset = {
  id: 4,
  user_id: 1,
  name: "Mine",
  params: toApiParams(INITIAL_STATE, resolved),
  created_at: "",
  updated_at: "",
};
const SHARED: SavedPreset = { ...MINE, id: 5, user_id: null, name: "Household" };

const bodyOf = (call: unknown[] | undefined) => (call?.[1] as { body: unknown }).body;
const mutate = (m: "POST" | "PUT" | "DELETE") => apiClient[m] as ReturnType<typeof vi.fn>;

function Harness({ userId = 1, state = INITIAL_STATE, initial = null }: { userId?: number | null; state?: ForecastState; initial?: PresetRef | null }) {
  const [selected, setSelected] = React.useState<PresetRef | null>(initial);
  return (
    <PresetBar
      userId={userId}
      builtin={BUILTIN}
      saved={[MINE, SHARED]}
      selected={selected}
      state={state}
      resolved={resolved}
      onSelect={setSelected}
      onSaved={(p) => setSelected({ kind: "saved", id: p.id })}
    />
  );
}
const renderBar = (props: React.ComponentProps<typeof Harness> = {}) =>
  render(
    <Wrapper>
      <Harness {...props} />
    </Wrapper>,
  );

afterEach(() => vi.resetAllMocks());

describe("PresetBar", () => {
  it("lists built-in and saved presets, labelling shared ones", () => {
    renderBar();
    const options = screen.getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Custom", "Moderate", "Mine", "Household (shared)"]);
  });

  it("offers rename and delete only for a saved preset", async () => {
    renderBar();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Preset" }), "builtin:moderate");
    expect(screen.getByText(/Moderate preset\. Built-in presets set only these four rates/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rename" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Preset" }), "saved:4");
    expect(screen.getByRole("button", { name: "Rename" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("saves for the user, or as shared when asked, and rejects an empty name", async () => {
    mutate("POST").mockResolvedValue({ data: { ...MINE, id: 8, name: "New" }, error: undefined });
    renderBar();
    await userEvent.click(screen.getByRole("button", { name: "Save as preset…" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Enter a name for the preset.")).toBeInTheDocument();
    expect(mutate("POST")).not.toHaveBeenCalled();

    await userEvent.type(screen.getByRole("textbox", { name: "Preset name" }), "  New  ");
    await userEvent.click(screen.getByRole("checkbox", { name: "Share with everyone" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mutate("POST")).toHaveBeenCalledTimes(1));
    expect(bodyOf(mutate("POST").mock.calls[0] as unknown[])).toMatchObject({ name: "New", user_id: null });
    // The form closes and the new preset is selected.
    await waitFor(() => expect(screen.queryByRole("form", { name: "Save preset" })).not.toBeInTheDocument());
  });

  it("shows the server's message when a name is taken", async () => {
    mutate("POST").mockResolvedValue({ data: undefined, error: { detail: "a preset named 'Mine' already exists" } });
    renderBar();
    await userEvent.click(screen.getByRole("button", { name: "Save as preset…" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Preset name" }), "Mine");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findAllByText(/already exists/)).length).toBeGreaterThan(0);
    expect(screen.getByRole("form", { name: "Save preset" })).toBeInTheDocument();
  });

  it("renames the selected saved preset", async () => {
    mutate("PUT").mockResolvedValue({ data: { ...MINE, name: "Renamed" }, error: undefined });
    renderBar({ initial: { kind: "saved", id: 4 } });
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: "Preset name" });
    expect(input).toHaveValue("Mine");
    await userEvent.clear(input);
    await userEvent.type(input, "Renamed");
    await userEvent.click(within(screen.getByRole("form", { name: "Rename preset" })).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(mutate("PUT")).toHaveBeenCalledTimes(1));
    expect(mutate("PUT")).toHaveBeenCalledWith("/net-worth/forecast/presets/{id}", {
      params: { path: { id: 4 } },
      body: { name: "Renamed" },
    });
  });

  it("offers to update a modified saved preset with the current scenario", async () => {
    mutate("PUT").mockResolvedValue({ data: MINE, error: undefined });
    const changed = { ...INITIAL_STATE, returnPct: 9 };
    renderBar({ initial: { kind: "saved", id: 4 }, state: changed });
    expect(screen.getByText("Modified from Mine")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Update “Mine”" }));
    await waitFor(() => expect(mutate("PUT")).toHaveBeenCalledTimes(1));
    expect((bodyOf(mutate("PUT").mock.calls[0] as unknown[]) as { params: unknown }).params).toMatchObject({ return_pct: 9, horizon_years: 10 });
  });

  it("confirms before deleting, then deselects the preset", async () => {
    mutate("DELETE").mockResolvedValue({ error: undefined });
    renderBar({ initial: { kind: "saved", id: 4 } });
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mutate("DELETE")).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete preset" }));
    await waitFor(() => expect(mutate("DELETE")).toHaveBeenCalledWith("/net-worth/forecast/presets/{id}", { params: { path: { id: 4 } } }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Preset" })).toHaveValue(""));
  });

  it("saves as shared without a checkbox when no user is selected", async () => {
    renderBar({ userId: null });
    await userEvent.click(screen.getByRole("button", { name: "Save as preset…" }));
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getByText(/Saved for all users/)).toBeInTheDocument();
  });
});
