"use client";

import { CircuitBoard, KeyRound, ShieldCheck, Sparkles } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const setToken = useAuth((s) => s.setToken);
  const token = useAuth((s) => s.token);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (token) router.replace("/");
  }, [token, router]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      // Validate the token by hitting any auth-required endpoint.
      await api.strategies(value.trim());
      setToken(value.trim());
      router.push("/");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Token rejected by API (401).  Re-mint and try again.");
      } else {
        setError(`Could not reach API: ${(err as Error).message}`);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gradient-mesh min-h-screen">
      <div className="flex min-h-screen items-center justify-center px-4">
        <div className="grid w-full max-w-5xl gap-8 lg:grid-cols-2">
          {/* Left column: marketing / context */}
          <div className="hidden flex-col justify-between lg:flex">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/15 text-primary">
                <CircuitBoard className="h-5 w-5" />
              </div>
              <div>
                <h1 className="text-xl font-semibold">Fincept Terminal</h1>
                <p className="text-xs text-muted-foreground">
                  Operator console
                </p>
              </div>
            </div>

            <div className="space-y-6">
              <h2 className="text-3xl font-semibold leading-tight tracking-tight">
                Trade safely.
                <br />
                <span className="text-primary">Observe everything.</span>
              </h2>
              <p className="max-w-md text-sm text-muted-foreground">
                Real-time positions, predictions, decisions, fills, and risk
                signals — every market event traceable to its source agent
                and decision id.
              </p>

              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="warn">PAPER MODE</Badge>
                <Badge variant="muted">
                  <ShieldCheck className="mr-1 h-3 w-3" />
                  Risk gate active
                </Badge>
                <Badge variant="muted">
                  <Sparkles className="mr-1 h-3 w-3" />
                  Live WebSocket feed
                </Badge>
              </div>
            </div>

            <p className="text-xs text-muted-foreground">
              v0.1.0 · Internal use only · Do not commit production tokens.
            </p>
          </div>

          {/* Right column: token entry */}
          <Card className="border-border/40">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
                <KeyRound className="h-4 w-4" />
                Sign in
              </CardTitle>
              <CardDescription>
                Paste your operator JWT.  Tokens are minted via{" "}
                <code className="rounded bg-muted px-1 py-0.5">
                  api.auth.encode_token(...)
                </code>{" "}
                in the API service.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={submit} className="space-y-4">
                <div className="space-y-2">
                  <label
                    htmlFor="token"
                    className="text-xs uppercase tracking-wider text-muted-foreground"
                  >
                    Bearer token
                  </label>
                  <Input
                    id="token"
                    type="password"
                    autoComplete="off"
                    autoFocus
                    placeholder="eyJhbGciOiJIUzI1NiIsInR…"
                    className="font-mono text-xs"
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                  />
                </div>
                {error ? (
                  <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                    {error}
                  </div>
                ) : null}
                <Button
                  type="submit"
                  disabled={!value.trim() || busy}
                  size="lg"
                  className="w-full"
                >
                  {busy ? "Verifying…" : "Continue"}
                </Button>
                <p className="text-[11px] leading-relaxed text-muted-foreground">
                  The token is stored in <code>localStorage</code> for v1.
                  Phase H replaces this with httpOnly cookies + OAuth flow.
                </p>
              </form>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
