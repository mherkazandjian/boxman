import yaml

with open('../demo_cluster.yml') as fobj:
    conf = yaml.safe_load(fobj.read())

print(conf)
print('done')
