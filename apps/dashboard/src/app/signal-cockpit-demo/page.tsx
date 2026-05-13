import { SignalCockpitDemo } from "@/features/signal-cockpit-demo/signal-cockpit-demo";

export const metadata = {
  title: "Fincept Signal Cockpit Demo",
  description:
    "Standalone mock UI based on the merged Fincept Signal Cockpit visualization specification.",
};

export default function SignalCockpitDemoPage() {
  return <SignalCockpitDemo />;
}
