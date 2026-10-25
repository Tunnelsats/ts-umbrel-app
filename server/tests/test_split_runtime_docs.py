from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_split_runtime_verify_script_and_docs_contract():
    verify_source = (REPO_ROOT / "tunnelsats" / "scripts" / "verify.sh").read_text()
    assert "tunnelsats-web" in verify_source
    assert "tunnelsats-daemon" in verify_source
    assert "docker exec tunnelsats " not in verify_source

    readme = (REPO_ROOT / "README.md").read_text()
    faq = (REPO_ROOT / "FAQ.md").read_text()
    web = (REPO_ROOT / "web" / "index.html").read_text()

    for source in (readme, faq, web):
        assert "tunnelsats-web" in source
        assert "tunnelsats-daemon" in source
        assert "docker exec tunnelsats wg show" not in source
    assert "Umbrel host itself at `http://127.0.0.1:9740`" not in readme
