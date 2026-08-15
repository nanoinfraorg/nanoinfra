import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SecretForm, privateKeyProblem } from "@/components/secrets/SecretForm";

/**
 * An operator lost hours to this form. The value field was a single-line password input, so an SSH
 * private key could not be pasted into it. They stored the public half first, and then a private
 * key whose newlines the input had replaced with spaces. Both saved without a word, and both
 * answered `Permission denied` from the host, which reads as a server problem.
 *
 * So the field takes several lines, and it refuses a value that no ssh client could parse.
 */

describe("the ssh key check", () => {
  it("accepts a private key", () => {
    const key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbn\n-----END OPENSSH PRIVATE KEY-----";

    expect(privateKeyProblem("ssh_key", key)).toBeNull();
  });

  it("names the public half when the operator pastes it", () => {
    const problem = privateKeyProblem("ssh_key", "ssh-ed25519 AAAAC3Nza alberto@BarraHome");

    expect(problem).toMatch(/public key/i);
    expect(problem).toMatch(/private/i);
  });

  it("catches a key whose line breaks are gone", () => {
    const collapsed =
      "-----BEGIN OPENSSH PRIVATE KEY----- b3Blbn -----END OPENSSH PRIVATE KEY-----";

    expect(privateKeyProblem("ssh_key", collapsed)).toMatch(/line breaks/i);
  });

  it("leaves another kind alone, because a password holds anything", () => {
    expect(privateKeyProblem("password", "ssh-ed25519 AAAA x@y")).toBeNull();
  });

  it("says nothing about an empty field", () => {
    expect(privateKeyProblem("ssh_key", "")).toBeNull();
  });
});

describe("SecretForm", () => {
  it("takes a value over several lines", () => {
    render(<SecretForm secret={null} onBack={() => {}} onSave={vi.fn()} />);

    const field = screen.getByPlaceholderText(/Enter secret value/i);
    expect(field.tagName).toBe("TEXTAREA");
  });

  it("refuses to save a public key and says why", async () => {
    const onSave = vi.fn();
    render(<SecretForm secret={null} onBack={() => {}} onSave={onSave} />);

    fireEvent.change(screen.getByPlaceholderText(/prod-db-password/i), {
      target: { value: "barrahome" },
    });
    fireEvent.change(screen.getByLabelText(/kind/i) ?? screen.getAllByRole("combobox")[0], {
      target: { value: "ssh_key" },
    });
    fireEvent.change(
      screen.getByPlaceholderText(/BEGIN OPENSSH PRIVATE KEY|Enter secret value/i),
      { target: { value: "ssh-ed25519 AAAAC3Nza alberto@BarraHome" } },
    );

    expect(await screen.findByText(/public key/i)).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });
});
