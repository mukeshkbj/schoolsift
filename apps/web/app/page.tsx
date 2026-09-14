import Link from "next/link";
import SignIn from "../components/SignIn";

const cognito = process.env.NEXT_PUBLIC_SCHOOLSIFT_AUTH_MODE === "cognito";

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
        <ol className="landing-steps">
          <li className="landing-step">
            <span className="landing-step-num" aria-hidden="true">1</span>
            Connect your school inboxes — Gmail, Outlook, as many as you
            need.
          </li>
          <li className="landing-step">
            <span className="landing-step-num" aria-hidden="true">2</span>
            Trust the senders that are your school; everything else stays
            headers-only.
          </li>
          <li className="landing-step">
            <span className="landing-step-num" aria-hidden="true">3</span>
            Review what needs you — summary, evidence, deadline, a draft
            reply or calendar event. You approve before anything is sent.
          </li>
        </ol>
        <p className="landing-safety">
          Payments and signatures are never automated.
        </p>
        <div className="landing-sift" aria-hidden="true">
          <span className="sift-node" />
          <span className="sift-line" />
          <span className="sift-node" />
          <span className="sift-line" />
          <span className="sift-node" />
        </div>
        {cognito ? (
          <SignIn />
        ) : (
          <Link href="/app" className="btn btn-primary btn-hero">
            Open SchoolSift
          </Link>
        )}
        <p className="landing-note">
          Local-first: your data stays in a local database until you connect an
          inbox.
        </p>
      </div>
    </main>
  );
}
