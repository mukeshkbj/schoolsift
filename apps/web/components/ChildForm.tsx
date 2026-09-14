"use client";

type Props = {
  busy: boolean;
  onAdd: (name: string, school: string, grade: string) => Promise<void>;
};

export default function ChildForm({ busy, onAdd }: Props) {
  return (
    <form
      className="setup-form"
      onSubmit={(e) => {
        e.preventDefault();
        const fd = new FormData(e.currentTarget);
        void onAdd(
          String(fd.get("name") ?? ""),
          String(fd.get("school") ?? ""),
          String(fd.get("grade") ?? ""),
        );
        e.currentTarget.reset();
      }}
    >
      <label className="field">
        <span className="field-label">Child name</span>
        <input name="name" type="text" required autoComplete="off" />
      </label>
      <label className="field">
        <span className="field-label">School</span>
        <input name="school" type="text" required autoComplete="off" />
      </label>
      <label className="field">
        <span className="field-label">Grade</span>
        <input name="grade" type="text" required autoComplete="off" />
      </label>
      <button
        type="submit"
        className="btn btn-primary"
        disabled={busy}
        aria-busy={busy}
      >
        Add child
        {busy && <span className="btn-busy" aria-hidden="true" />}
      </button>
    </form>
  );
}
