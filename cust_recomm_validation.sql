
SELECT service_code, service_name, is_active, category,
       resource_type, metrics_table_suffix, resources_table_suffix,
       service_name_filter, recommender_class, retail_api_service_name
FROM service_configuration
WHERE service_code = 'ADF' AND cloud_provider = 'azure';


SELECT rule_code, severity, priority, is_active,
       evaluation_logic->'conditions' AS conditions,
       cost_model
FROM base_finops_threshold_rules
WHERE service_name = 'Azure Data Factory v2' AND cloud_provider = 'azure'
ORDER BY priority;

SELECT rule_code, client_id, subscription_id, is_active
FROM finops_threshold_rules
WHERE service_name = 'Azure Data Factory v2' AND cloud_provider = 'azure'
ORDER BY rule_code;

SELECT *
--resource_id, rule_code, metric_name, savings,       recommendation
FROM custom_recommendations
WHERE cloud_name = 'Azure' and client_id='herbalife_testing'
--  AND rule_code LIKE 'ADF_%'
ORDER BY savings DESC;

select * from base_finops_threshold_rules bftr where service_name='Azure Data Factory v2'

select * from finops_threshold_rules ftr where service_name='Azure Data Factory v2'