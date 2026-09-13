import Link from "next/link";

export default function Landing() {
  return (
    <main className="landing">
      <div className="landing-inner">
        <p className="landing-kicker">SchoolSift</p>
        <h1 className="landing-headline">
          Every school email, turned into the next right action.
        </h1>
        <p className="landing-sub">
          SchoolSift reads confirmed school senders and prepares a reviewable
          Action Packet — the summary, the deadline, the draft reply, the
          calendar event — so a busy parent only ever has to decide. Nothing is
          sent or scheduled without your approval.
        </p>
        <div className="landing-sift" aria-hidden="true">
          <span className="sift-node" />
          <span className="sift-line" />
          <span className="sift-node" />
          <span className="sift-line" />
          <span className="sift-node" />
        </div>
        <Link href="/demo" className="btn btn-primary btn-hero">
          Try the demo
        </Link>
        <p className="landing-note">
          Fully synthetic demo data — no inbox access needed, nothing real is
          ever sent.
        </p>
      </div>
    </main>
  );
}
