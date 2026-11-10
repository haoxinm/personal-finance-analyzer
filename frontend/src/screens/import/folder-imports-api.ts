import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "@/api/client";

/** Hooks of the "Folder imports" panel: PDFs put into the documents folder by hand are imported by
 * a background scan (`/api/watch/*`). */

export type WatchStatus = components["schemas"]["WatchStatus"];
export type WatchFile = components["schemas"]["WatchFile"];
export type WatchView = "recent" | "problems";

const WATCH_KEY = ["watch"] as const;

/** Poll quickly while a scan runs (so "Scanning..." ends promptly), slowly otherwise. */
export function useWatchStatus() {
  return useQuery({
    queryKey: [...WATCH_KEY, "status"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/watch/status");
      throwIfError(error);
      if (!data) throw new Error("The server returned no folder-import status.");
      return data;
    },
    refetchInterval: (query) => (query.state.data?.running ? 1500 : 15000),
  });
}

export function useWatchFiles(view: WatchView) {
  return useQuery({
    queryKey: [...WATCH_KEY, "files", view],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/watch/files", {
        params: { query: { view, limit: view === "recent" ? 10 : 100 } },
      });
      throwIfError(error);
      return data ?? { items: [], total: 0 };
    },
    refetchInterval: 15000,
  });
}

/** "Scan now": starts a scan in the background; the status query shows it running. */
export function useScanNow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/watch/scan");
      throwIfError(error);
      if (!data) throw new Error("The server returned no answer.");
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData([...WATCH_KEY, "status"], data.status);
      void queryClient.invalidateQueries({ queryKey: WATCH_KEY });
    },
  });
}

/** Retry: forget a problem so the next scan looks at the file again. */
export function useRetryWatchFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (fileId: number) => {
      const { error } = await apiClient.POST("/watch/files/{file_id}/retry", {
        params: { path: { file_id: fileId } },
      });
      throwIfError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: WATCH_KEY });
    },
  });
}
