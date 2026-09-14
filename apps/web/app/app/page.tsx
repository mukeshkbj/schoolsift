import type { Metadata } from "next";
import SchoolSiftApp from "../../components/SchoolSiftApp";

export const metadata: Metadata = {
  title: "SchoolSift",
};

export default function AppPage() {
  return <SchoolSiftApp />;
}
