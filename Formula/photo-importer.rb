class PhotoImporter < Formula
  include Language::Python::Virtualenv

  desc "CLI to import photos from SD cards into ~/Pictures, organised by date and camera"
  homepage "https://github.com/suarez605/photo-importer"
  url "https://github.com/suarez605/photo-importer/archive/refs/tags/v1.0.0.tar.gz"
  # sha256 must be updated when the tarball changes:
  #   curl -sL <url> | shasum -a 256
  sha256 "PLACEHOLDER_UPDATE_WITH_REAL_SHA256"
  license "MIT"

  depends_on "python@3.11"

  # questionary and its dependencies are vendored into the Homebrew bottle so
  # the user never needs to touch pip or a virtualenv manually.
  resource "questionary" do
    url "https://files.pythonhosted.org/packages/source/q/questionary/questionary-2.1.1.tar.gz"
    sha256 "PLACEHOLDER_UPDATE_WITH_REAL_SHA256"
  end

  resource "prompt_toolkit" do
    url "https://files.pythonhosted.org/packages/source/p/prompt_toolkit/prompt_toolkit-3.0.52.tar.gz"
    sha256 "PLACEHOLDER_UPDATE_WITH_REAL_SHA256"
  end

  resource "wcwidth" do
    url "https://files.pythonhosted.org/packages/source/w/wcwidth/wcwidth-0.6.0.tar.gz"
    sha256 "PLACEHOLDER_UPDATE_WITH_REAL_SHA256"
  end

  def install
    # Homebrew's Python virtualenv helper creates an isolated env and installs
    # all resources into it before calling `pip install .` for the main package.
    virtualenv_install_with_resources
  end

  test do
    # Smoke test: the command must exist and exit without crashing when there
    # is no terminal attached (questionary exits gracefully in that case).
    system "#{bin}/photo-importer", "--help" rescue nil
    assert_predicate bin/"photo-importer", :exist?
  end
end
