"use client";

import { useQueryClient } from "@tanstack/react-query";

import { useFinceptStream } from "@/lib/ws";

export function StreamInvalidator() {
  const queryClient = useQueryClient();
  useFinceptStream({
    topics: ["positions", "fills", "predictions", "alerts"],
    onFrame: (frame) => {
      if (frame.topic === "positions") {
        queryClient.invalidateQueries({ queryKey: ["positions"] });
        queryClient.invalidateQueries({ queryKey: ["strategies"] });
      }
      if (frame.topic === "fills") {
        queryClient.invalidateQueries({ queryKey: ["orders"] });
        queryClient.invalidateQueries({ queryKey: ["positions"] });
        queryClient.invalidateQueries({ queryKey: ["strategies"] });
      }
      if (frame.topic === "predictions") {
        queryClient.invalidateQueries({ queryKey: ["models"] });
      }
      if (frame.topic === "alerts") {
        queryClient.invalidateQueries({ queryKey: ["services"] });
        queryClient.invalidateQueries({ queryKey: ["health"] });
      }
    },
  });
  return null;
}
