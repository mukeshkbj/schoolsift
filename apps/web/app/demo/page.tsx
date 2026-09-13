import type { Metadata } from "next";
import DemoWorkspace from "../../components/DemoWorkspace";

export const metadata: Metadata = {
  title: "SchoolSift demo",
};

export default function DemoPage() {
  return <DemoWorkspace />;
}
