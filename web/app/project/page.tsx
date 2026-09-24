import { Suspense } from "react";
import ProjectView from "@/components/ProjectView";
import { Skeleton } from "@/components/ui";

export default function ProjectPage() {
  return (
    <Suspense fallback={<Skeleton lines={5} />}>
      <ProjectView />
    </Suspense>
  );
}
