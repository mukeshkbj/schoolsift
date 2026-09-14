"use client";

import { useEffect, useRef } from "react";
import type { BootstrapResponse } from "../lib/contracts";
import ChildForm from "./ChildForm";

export type SettingsSection = "household" | "children" | "caregivers";

type Props = {
  boot: BootstrapResponse;
  canEdit: boolean;
  isOwner: boolean;
  busyKey: string | null;
  inviteToken: string | null;
  inviteCopied: boolean;
  section: SettingsSection;
  onClose: () => void;
  onAddChild: (name: string, school: string, grade: string) => Promise<void>;
  onInvite: (email: string, role: "editor" | "viewer") => Promise<void>;
  onRevokeInvite: (invitationId: string) => Promise<void>;
  onChangeRole: (userId: string, role: string) => Promise<void>;
  onRemoveMember: (userId: string) => Promise<void>;
  onInviteCopied: () => void;
};

export default function SettingsDrawer({
  boot,
  canEdit,
  isOwner,
  busyKey,
  inviteToken,
  inviteCopied,
  section,
  onClose,
  onAddChild,
  onInvite,
  onRevokeInvite,
  onChangeRole,
  onRemoveMember,
  onInviteCopied,
}: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const householdRef = useRef<HTMLElement>(null);
  const childrenRef = useRef<HTMLElement>(null);
  const caregiversRef = useRef<HTMLElement>(null);

  useEffect(() => {
    dialogRef.current?.focus();
    const target = { household: householdRef, children: childrenRef, caregivers: caregiversRef }[
      section
    ];
    target.current?.scrollIntoView?.({ block: "start" });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, section]);

  const household = boot.household;

  return (
    <div className="settings-backdrop" onClick={onClose}>
      <div
        className="settings-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        tabIndex={-1}
        ref={dialogRef}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="settings-head">
          <h2 id="settings-title" className="rail-heading">
            Settings
          </h2>
          <button type="button" className="btn btn-quiet" onClick={onClose}>
            Close
          </button>
        </div>

        <section
          className="settings-section"
          aria-label="Household"
          ref={householdRef}
        >
          <h3 className="source-heading">Household</h3>
          {household !== null && (
            <dl className="settings-facts">
              <div className="settings-fact">
                <dt>Name</dt>
                <dd>{household.name}</dd>
              </div>
              <div className="settings-fact">
                <dt>Time zone</dt>
                <dd>{household.timezone}</dd>
              </div>
            </dl>
          )}
        </section>

        <section
          className="settings-section"
          aria-label="Children"
          ref={childrenRef}
        >
          <h3 className="source-heading">Children</h3>
          {boot.children.length > 0 ? (
            <ol className="connection-list">
              {boot.children.map((c) => (
                <li key={c.id} className="connection-item">
                  <span className="connection-email">{c.name}</span>
                  <span className="connection-meta">
                    {c.school} · grade {c.grade}
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="connection-empty">No children added yet.</p>
          )}
          {canEdit && (
            <ChildForm busy={busyKey === "child"} onAdd={onAddChild} />
          )}
        </section>

        <section
          className="settings-section"
          aria-label="Caregivers"
          ref={caregiversRef}
        >
          <h3 className="source-heading">Caregivers</h3>
          {boot.members.length > 0 && (
            <ol className="connection-list">
              {boot.members.map((m) => (
                <li key={m.user_id} className="connection-item">
                  <span className="connection-email">{m.email}</span>
                  <span className="connection-meta">{m.role}</span>
                  {isOwner && (
                    <div className="source-actions">
                      <label className="field member-role">
                        <span className="field-label">Role</span>
                        <select
                          value={m.role}
                          disabled={busyKey === `role:${m.user_id}`}
                          onChange={(e) =>
                            void onChangeRole(m.user_id, e.target.value)
                          }
                        >
                          <option value="owner">owner</option>
                          <option value="editor">editor</option>
                          <option value="viewer">viewer</option>
                        </select>
                      </label>
                      <button
                        type="button"
                        className="btn btn-quiet"
                        disabled={busyKey === `remove:${m.user_id}`}
                        onClick={() => void onRemoveMember(m.user_id)}
                      >
                        Remove
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ol>
          )}
          {isOwner ? (
            <>
              <form
                className="setup-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  const fd = new FormData(e.currentTarget);
                  void onInvite(
                    String(fd.get("email") ?? ""),
                    fd.get("role") === "editor" ? "editor" : "viewer",
                  );
                  e.currentTarget.reset();
                }}
              >
                <label className="field">
                  <span className="field-label">Invite caregiver email</span>
                  <input name="email" type="email" required />
                </label>
                <label className="field">
                  <span className="field-label">Role</span>
                  <select name="role" defaultValue="editor">
                    <option value="editor">editor</option>
                    <option value="viewer">viewer</option>
                  </select>
                </label>
                <button
                  type="submit"
                  className="btn btn-secondary"
                  disabled={busyKey === "invite"}
                  aria-busy={busyKey === "invite"}
                >
                  Create invite
                </button>
              </form>
              {inviteToken !== null && (
                <div className="invite-token-box">
                  <p className="connection-meta">
                    No email is sent yet — copy this invite link text and
                    share it with the caregiver. It is shown only once.
                  </p>
                  <input
                    readOnly
                    className="invite-token"
                    value={inviteToken}
                    aria-label="Invitation token"
                    onFocus={(e) => e.currentTarget.select()}
                  />
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => {
                      void navigator.clipboard
                        ?.writeText(inviteToken)
                        .then(() => onInviteCopied());
                    }}
                  >
                    {inviteCopied ? "Copied" : "Copy invite"}
                  </button>
                </div>
              )}
              {boot.invitations.length > 0 && (
                <ol className="connection-list">
                  {boot.invitations.map((i) => (
                    <li key={i.id} className="connection-item">
                      <span className="connection-email">{i.email}</span>
                      <span className="connection-meta">
                        {i.role} · {i.status}
                      </span>
                      {i.status === "pending" && (
                        <button
                          type="button"
                          className="btn btn-quiet"
                          disabled={busyKey === `revoke:${i.id}`}
                          onClick={() => void onRevokeInvite(i.id)}
                        >
                          Revoke invite
                        </button>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </>
          ) : (
            <p className="connection-empty">
              Only owners manage caregivers.
            </p>
          )}
        </section>
      </div>
    </div>
  );
}
