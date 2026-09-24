import { Suspense } from "react";
import RunView from "@/components/RunView";
import { Skeleton } from "@/components/ui";

export default function RunPage() {
  return (
    <Suspense fallback={<Skeleton lines={5} />}>
      <RunView />
    </Suspense>
  );
}
