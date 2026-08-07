from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_verify_script_targets_both_split_services():
    source = (REPO_ROOT / "tunnelsats" / "scripts" / "verify.sh").read_text()

    assert "tunnelsats-web" in source
    assert "tunnelsats-daemon" in source
    assert "docker exec tunnelsats " not in source


def test_readme_does_not_advertise_removed_host_loopback_backend():
    readme = (REPO_ROOT / "README.md").read_text()
    faq = (REPO_ROOT / "FAQ.md").read_text()
    web = (REPO_ROOT / "web" / "index.html").read_text()

    for source in (readme, faq, web):
        assert "tunnelsats-web" in source
        assert "tunnelsats-daemon" in source
        assert "docker exec tunnelsats wg show" not in source
    assert "Umbrel host itself at `http://127.0.0.1:9740`" not in readme
