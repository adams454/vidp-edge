# Checklist avant la démo (à cocher sur les vraies machines, au moins une fois)
- [ ] Poste de contrôle : `ansible --version`, `python3 -c "import yaml, cryptography"`, clé SSH acceptée par les machines
- [ ] `ansible-playbook infra/ansible/site.yml --syntax-check` sans erreur
- [ ] `EDGE_HOSTS=… CLOUD_HOST=… ./scripts/deploy.sh all` va au bout depuis des VM vierges ; noter la durée affichée (< 10 min)
- [ ] Page publique `http://IP_CLOUD/` visible depuis un autre réseau ; `./scripts/deploy.sh load-start` : des vidéos
      apparaissent « en cours » puis « traitées » avec vignette, extrait, sous-titres et animaux
- [ ] La tâche Ansible « Connexion Edge -> Cloud vérifiée » passe (sinon : `EDGE_CIDRS` = IP vue par le Cloud, ou `CLOUD_FIREWALL=0`)
- [ ] `./scripts/wanctl.sh set --rate 5` : `docker exec vidp-wan tc -s qdisc show dev eth0` montre netem + tbf
- [ ] `./scripts/wanctl.sh down` : `uploader_wan_up` passe à 0 dans Grafana, le tampon monte ; `up` : il se vide, 0 doublon sur la page
- [ ] `cat /sys/class/powercap/intel-rapl:0/energy_uj` lisible (sinon l'exportateur annonce `source=model` : à signaler dans le rapport)
- [ ] Grafana : datasource « Cloud » joignable (port Prometheus du Cloud ouvert à l'IP Edge)
- [ ] `python3 scripts/run_scenarios.py --preset demo` puis `python3 scripts/analyze.py` produisent `results/summary.md`
- [ ] Avec AWS : `terraform -chdir=infra/terraform validate && terraform plan` ; si la création de rôle IAM est refusée,
      `export TF_VAR_instance_profile_name=LabInstanceProfile`
- [ ] Image pré-publiée (CI → GHCR) et `VIDP_REGISTRY` exporté : redéploiement chronométré plus court (pas de build ni de téléchargement des modèles)
