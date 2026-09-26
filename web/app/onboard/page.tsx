import { Suspense } from "react";
import OnboardView from "@/components/OnboardView";
import { Skeleton } from "@/components/ui";

export default function OnboardPage() {
  return (
    <Suspense fallback={<Skeleton lines={5} />}>
      <OnboardView />
    </Suspense>
  );
}
